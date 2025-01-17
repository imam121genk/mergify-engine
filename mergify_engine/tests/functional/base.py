# -*- encoding: utf-8 -*-
#
# Copyright © 2018—2021 Mergify SAS
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import asyncio
import copy
import datetime
import json
import os
import queue
import re
import shutil
import time
import unittest
from unittest import mock

import daiquiri
import github as pygithub
import httpx
import pytest
import vcr
import vcr.stubs.urllib3_stubs
import yaml

from mergify_engine import branch_updater
from mergify_engine import config
from mergify_engine import context
from mergify_engine import duplicate_pull
from mergify_engine import engine
from mergify_engine import gitter
from mergify_engine import subscription
from mergify_engine import user_tokens
from mergify_engine import utils
from mergify_engine import web
from mergify_engine import worker
from mergify_engine.clients import github
from mergify_engine.clients import github_app
from mergify_engine.clients import http


LOG = daiquiri.getLogger(__name__)
RECORD = bool(os.getenv("MERGIFYENGINE_RECORD", False))
CASSETTE_LIBRARY_DIR_BASE = "zfixtures/cassettes"
FAKE_DATA = "whatdataisthat"
FAKE_HMAC = utils.compute_hmac(FAKE_DATA.encode("utf8"))


class GitterRecorder(gitter.Gitter):
    def __init__(self, logger, cassette_library_dir, suffix):
        super(GitterRecorder, self).__init__(logger)
        self.cassette_path = os.path.join(cassette_library_dir, f"git-{suffix}.json")
        if RECORD:
            self.records = []
        else:
            self.load_records()

    def load_records(self):
        if not os.path.exists(self.cassette_path):
            raise RuntimeError(f"Cassette {self.cassette_path} not found")
        with open(self.cassette_path, "rb") as f:
            data = f.read().decode("utf8")
            self.records = json.loads(data)

    def save_records(self):
        with open(self.cassette_path, "wb") as f:
            data = json.dumps(self.records)
            f.write(data.encode("utf8"))

    async def __call__(self, *args, **kwargs):
        if RECORD:
            try:
                output = await super(GitterRecorder, self).__call__(*args, **kwargs)
            except gitter.GitError as e:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "exc": {
                            "returncode": e.returncode,
                            "output": e.output,
                        },
                    }
                )
                raise
            else:
                self.records.append(
                    {
                        "args": self.prepare_args(args),
                        "kwargs": self.prepare_kwargs(kwargs),
                        "out": output,
                    }
                )
            return output
        else:
            r = self.records.pop(0)
            if "exc" in r:
                raise gitter.GitError(
                    returncode=r["exc"]["returncode"],
                    output=r["exc"]["output"],
                )
            else:
                assert r["args"] == self.prepare_args(
                    args
                ), f'{r["args"]} != {self.prepare_args(args)}'
                assert r["kwargs"] == self.prepare_kwargs(
                    kwargs
                ), f'{r["kwargs"]} != {self.prepare_kwargs(kwargs)}'
                return r["out"]

    def prepare_args(self, args):
        return [arg.replace(self.tmp, "/tmp/mergify-gitter<random>") for arg in args]

    @staticmethod
    def prepare_kwargs(kwargs):
        if "_input" in kwargs:
            kwargs["_input"] = re.sub(r"://[^@]*@", "://<TOKEN>:@", kwargs["_input"])
        return kwargs

    async def cleanup(self):
        await super(GitterRecorder, self).cleanup()
        if RECORD:
            self.save_records()


class EventReader:
    FORWARDER_ENDPOINT = "https://test-forwarder.mergify.io/events-testing"

    def __init__(self, app):
        self._app = app
        self._session = http.AsyncClient()
        self._handled_events = queue.Queue()
        self._counter = 0

    async def drain(self):
        # NOTE(sileht): Drop any pending events still on the server
        r = await self._session.request(
            "DELETE",
            self.FORWARDER_ENDPOINT,
            data=FAKE_DATA,
            headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
        )
        r.raise_for_status()

    async def wait_for(self, event_type, expected_payload, timeout=15 if RECORD else 2):
        LOG.log(
            42,
            "WAITING FOR %s/%s: %s",
            event_type,
            expected_payload.get("action"),
            expected_payload,
        )

        started_at = time.monotonic()
        while time.monotonic() - started_at < timeout:
            try:
                event = self._handled_events.get(block=False)
                await self._forward_to_engine_api(event)
            except queue.Empty:
                for event in await self._get_events():
                    self._handled_events.put(event)
                else:
                    if RECORD:
                        time.sleep(1)
                continue

            if event["type"] == event_type and self._match(
                event["payload"], expected_payload
            ):
                return

        raise Exception(
            f"Never got event `{event_type}` with payload `{expected_payload}` (timeout)"
        )

    @classmethod
    def _match(cls, data, expected_data):
        if isinstance(expected_data, dict):
            for key, expected in expected_data.items():
                if key not in data:
                    return False
                if not cls._match(data[key], expected):
                    return False
            return True
        else:
            return data == expected_data

    async def _get_events(self):
        # NOTE(sileht): we use a counter to make each call unique in cassettes
        self._counter += 1
        return (
            await self._session.request(
                "GET",
                f"{self.FORWARDER_ENDPOINT}?counter={self._counter}",
                data=FAKE_DATA,
                headers={"X-Hub-Signature": "sha1=" + FAKE_HMAC},
            )
        ).json()

    async def _forward_to_engine_api(self, event):
        payload = event["payload"]
        if event["type"] in ["check_run", "check_suite"]:
            extra = (
                f"/{payload[event['type']].get('status')}"
                f"/{payload[event['type']].get('conclusion')}"
            )
        elif event["type"] == "status":
            extra = f"/{payload.get('state')}"
        else:
            extra = ""
        LOG.log(
            42,
            "EVENT RECEIVED %s/%s%s: %s",
            event["type"],
            payload.get("action"),
            extra,
            self._remove_useless_links(copy.deepcopy(event)),
        )
        r = await self._app.post(
            "/event",
            headers={
                "X-GitHub-Event": event["type"],
                "X-GitHub-Delivery": "123456789",
                "X-Hub-Signature": "sha1=whatever",
                "Content-type": "application/json",
            },
            data=json.dumps(payload),
        )
        return r

    @classmethod
    def _remove_useless_links(cls, data):
        if isinstance(data, dict):
            data.pop("installation", None)
            data.pop("sender", None)
            data.pop("repository", None)
            data.pop("base", None)
            data.pop("head", None)
            data.pop("id", None)
            data.pop("node_id", None)
            data.pop("tree_id", None)
            data.pop("_links", None)
            data.pop("user", None)
            data.pop("body", None)
            data.pop("after", None)
            data.pop("before", None)
            data.pop("app", None)
            data.pop("timestamp", None)
            data.pop("external_id", None)
            if "organization" in data:
                data["organization"].pop("description", None)
            if "check_run" in data:
                data["check_run"].pop("checks_suite", None)
            for key, value in list(data.items()):
                if key.endswith("url"):
                    del data[key]
                elif key.endswith("_at"):
                    del data[key]
                else:
                    data[key] = cls._remove_useless_links(value)
            return data
        elif isinstance(data, list):
            return [cls._remove_useless_links(elem) for elem in data]
        else:
            return data


@pytest.mark.usefixtures("logger_checker")
class FunctionalTestBase(unittest.IsolatedAsyncioTestCase):
    # NOTE(sileht): The repository have been manually created in mergifyio-testing
    # organization and then forked in mergify-test2 user account
    REPO_ID = 258840104
    REPO_NAME = "functional-testing-repo"
    FORK_PERSONAL_TOKEN = config.EXTERNAL_USER_PERSONAL_TOKEN
    SUBSCRIPTION_ACTIVE = False

    # To run tests on private repository, you can use:
    # REPO_NAME = "functional-testing-repo-private"
    # FORK_PERSONAL_TOKEN = config.ORG_USER_PERSONAL_TOKEN
    # SUBSCRIPTION_ACTIVE = True

    async def asyncSetUp(self):
        super(FunctionalTestBase, self).setUp()
        self.existing_labels = []
        self.pr_counter = 0
        self.git_counter = 0
        self.cassette_library_dir = os.path.join(
            CASSETTE_LIBRARY_DIR_BASE, self.__class__.__name__, self._testMethodName
        )

        # Recording stuffs
        if RECORD:
            if os.path.exists(self.cassette_library_dir):
                shutil.rmtree(self.cassette_library_dir)
            os.makedirs(self.cassette_library_dir)

        self.recorder = vcr.VCR(
            cassette_library_dir=self.cassette_library_dir,
            record_mode="all" if RECORD else "none",
            match_on=["method", "uri"],
            ignore_localhost=True,
            filter_headers=[
                ("Authorization", "<TOKEN>"),
                ("X-Hub-Signature", "<SIGNATURE>"),
                ("User-Agent", None),
                ("Accept-Encoding", None),
                ("Connection", None),
            ],
            before_record_response=self.response_filter,
            custom_patches=(
                (pygithub.MainClass, "HTTPSConnection", vcr.stubs.VCRHTTPSConnection),
            ),
        )

        if RECORD:
            github.CachedToken.STORAGE = {}
        else:
            # Never expire token during replay
            mock.patch.object(
                github_app, "get_or_create_jwt", return_value="<TOKEN>"
            ).start()
            mock.patch.object(
                github.GithubAppInstallationAuth,
                "get_access_token",
                return_value="<TOKEN>",
            ).start()

            # NOTE(sileht): httpx pyvcr stubs does not replay auth_flow as it directly patch client.send()
            # So anything occurring during auth_flow have to be mocked during replay
            def get_auth(owner_name=None, owner_id=None, auth=None):
                if auth is None:
                    auth = github.get_auth(owner_name, owner_id)
                    auth.installation = {
                        "id": config.INSTALLATION_ID,
                    }
                    auth.permissions_need_to_be_updated = False
                    auth.owner_id = config.TESTING_ORGANIZATION_ID
                    auth.owner = config.TESTING_ORGANIZATION
                return auth

            def github_aclient(owner_name=None, owner_id=None, auth=None):
                return github.AsyncGithubInstallationClient(
                    get_auth(owner_name, owner_id, auth)
                )

            mock.patch.object(github, "aget_client", github_aclient).start()

        with open(engine.mergify_rule_path, "r") as f:
            engine.MERGIFY_RULE = yaml.safe_load(
                f.read().replace("mergify[bot]", "mergify-test[bot]")
            )

        mock.patch.object(branch_updater.gitter, "Gitter", self.get_gitter).start()
        mock.patch.object(duplicate_pull.gitter, "Gitter", self.get_gitter).start()

        if not RECORD:
            # NOTE(sileht): Don't wait exponentialy during replay
            mock.patch.object(
                context.Context._ensure_complete.retry, "wait", None
            ).start()

        # Web authentification always pass
        mock.patch("hmac.compare_digest", return_value=True).start()

        branch_prefix_path = os.path.join(self.cassette_library_dir, "branch_prefix")

        if RECORD:
            self.BRANCH_PREFIX = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
            with open(branch_prefix_path, "w") as f:
                f.write(self.BRANCH_PREFIX)
        else:
            with open(branch_prefix_path, "r") as f:
                self.BRANCH_PREFIX = f.read()

        self.master_branch_name = self.get_full_branch_name("master")

        self.git = self.get_gitter(LOG)
        await self.git.init()
        self.addAsyncCleanup(self.git.cleanup)

        await web.startup()
        self.app = httpx.AsyncClient(app=web.app, base_url="http://localhost")

        await self.clear_redis_cache()
        self.redis_cache = await utils.create_aredis_for_cache(max_idle_time=0)
        self.subscription = subscription.Subscription(
            self.redis_cache,
            config.TESTING_ORGANIZATION_ID,
            self.SUBSCRIPTION_ACTIVE,
            "You're not nice",
            frozenset(
                getattr(subscription.Features, f)
                for f in subscription.Features.__members__
            )
            if self.SUBSCRIPTION_ACTIVE
            else frozenset(),
        )
        await self.subscription.save_subscription_to_cache()
        self.user_tokens = user_tokens.UserTokens(
            self.redis_cache,
            config.TESTING_ORGANIZATION_ID,
            {
                "mergify-test1": config.ORG_ADMIN_GITHUB_APP_OAUTH_TOKEN,
                "mergify-test3": config.ORG_USER_PERSONAL_TOKEN,
            },
        )
        await self.user_tokens.save_to_cache()

        # Let's start recording
        cassette = self.recorder.use_cassette("http.json")
        cassette.__enter__()
        self.addCleanup(cassette.__exit__)

        integration = pygithub.GithubIntegration(
            config.INTEGRATION_ID, config.PRIVATE_KEY
        )
        self.installation_token = integration.get_access_token(
            config.INSTALLATION_ID
        ).token

        base_url = config.GITHUB_API_URL
        self.g_integration = pygithub.Github(self.installation_token, base_url=base_url)
        self.g_admin = pygithub.Github(
            config.ORG_ADMIN_PERSONAL_TOKEN, base_url=base_url
        )
        self.g_fork = pygithub.Github(self.FORK_PERSONAL_TOKEN, base_url=base_url)

        self.o_admin = self.g_admin.get_organization(config.TESTING_ORGANIZATION)
        self.o_integration = self.g_integration.get_organization(
            config.TESTING_ORGANIZATION
        )
        self.u_fork = self.g_fork.get_user()
        assert self.o_admin.login == "mergifyio-testing"
        assert self.o_integration.login == "mergifyio-testing"
        assert self.u_fork.login in ["mergify-test2", "mergify-test3"]

        self.r_o_admin = self.o_admin.get_repo(self.REPO_NAME)
        self.r_o_integration = self.o_integration.get_repo(self.REPO_NAME)
        self.r_fork = self.u_fork.get_repo(self.REPO_NAME)

        self.url_main = f"{config.GITHUB_URL}/{self.r_o_integration.full_name}"
        self.url_fork = (
            f"{config.GITHUB_URL}/{self.u_fork.login}/{self.r_o_integration.name}"
        )

        self.cli_integration = github.aget_client(config.TESTING_ORGANIZATION)
        self.addAsyncCleanup(self.cli_integration.aclose)
        self.installation_ctxt = context.Installation(
            config.TESTING_ORGANIZATION_ID,
            config.TESTING_ORGANIZATION,
            self.subscription,
            self.cli_integration,
            self.redis_cache,
        )
        self.repository_ctxt = context.Repository(
            self.installation_ctxt, self.REPO_NAME, self.r_o_integration.id
        )

        real_get_subscription = subscription.Subscription.get_subscription

        async def fake_retrieve_subscription_from_db(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return self.subscription
            return subscription.Subscription(
                redis_cache,
                owner_id,
                False,
                "We're just testing",
                set(),
            )

        async def fake_subscription(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return await real_get_subscription(redis_cache, owner_id)
            return subscription.Subscription(
                redis_cache,
                owner_id,
                False,
                "We're just testing",
                set(),
            )

        mock.patch(
            "mergify_engine.subscription.Subscription._retrieve_subscription_from_db",
            side_effect=fake_retrieve_subscription_from_db,
        ).start()

        mock.patch(
            "mergify_engine.subscription.Subscription.get_subscription",
            side_effect=fake_subscription,
        ).start()

        async def fake_retrieve_user_tokens_from_db(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return self.user_tokens
            return user_tokens.UserTokens(redis_cache, owner_id, {})

        real_get_user_tokens = user_tokens.UserTokens.get

        async def fake_user_tokens(redis_cache, owner_id):
            if owner_id == config.TESTING_ORGANIZATION_ID:
                return await real_get_user_tokens(redis_cache, owner_id)
            return user_tokens.UserTokens(redis_cache, owner_id, {})

        mock.patch(
            "mergify_engine.user_tokens.UserTokens._retrieve_from_db",
            side_effect=fake_retrieve_user_tokens_from_db,
        ).start()

        mock.patch(
            "mergify_engine.user_tokens.UserTokens.get",
            side_effect=fake_user_tokens,
        ).start()

        mock.patch(
            "github.MainClass.Installation.Installation.get_repos",
            return_value=[self.r_o_integration],
        ).start()

        self._event_reader = EventReader(self.app)
        await self._event_reader.drain()

        # NOTE(sileht): Prepare a fresh redis
        await self.clear_redis_stream()

    @staticmethod
    async def clear_redis_stream():
        redis_stream = await utils.create_aredis_for_stream(max_idle_time=0)
        await redis_stream.flushall()

    @staticmethod
    async def clear_redis_cache():
        async with utils.aredis_for_cache() as redis_stream:
            await redis_stream.flushall()

    async def asyncTearDown(self):
        super(FunctionalTestBase, self).tearDown()

        # NOTE(sileht): Wait a bit to ensure all remaining events arrive. And
        # also to avoid the "git clone fork" failure that Github returns when
        # we create repo too quickly
        if RECORD:
            time.sleep(0.5)

            self.r_o_admin.edit(default_branch="master")

            branches = list(self.r_o_admin.get_git_matching_refs("heads/20"))
            branches.extend(self.r_o_admin.get_git_matching_refs("heads/mergify"))
            try:
                branches.extend(self.r_fork.get_git_matching_refs("heads/20"))
                branches.extend(self.r_fork.get_git_matching_refs("heads/mergify"))
            except pygithub.GithubException as e:
                if e.data["message"] != "Git Repository is empty.":
                    raise
            for branch in branches:
                if "branch_protection" in branch.ref:
                    try:
                        self.branch_protection_unprotect(branch.ref)
                    except pygithub.GithubException as e:
                        if e.status != 404:
                            raise

                branch.delete()

            for label in self.r_o_admin.get_labels():
                label.delete()

            for pull in self.r_o_admin.get_pulls():
                pull.edit(state="closed")

        await self.app.aclose()
        await web.shutdown()

        await self._event_reader.drain()
        await self.clear_redis_stream()
        mock.patch.stopall()

    async def wait_for(self, *args, **kwargs):
        return await self._event_reader.wait_for(*args, **kwargs)

    @staticmethod
    async def _async_run_workers(timeout):
        w = worker.Worker(
            idle_sleep_time=0.42 if RECORD else 0.01, enabled_services=["stream"]
        )
        w.start()

        started_at = None
        while True:
            if w._redis_stream is None or (await w._redis_stream.zcard("streams")) > 0:
                started_at = None
            elif started_at is None:
                started_at = time.monotonic()
            elif time.monotonic() - started_at >= timeout:
                break
            await asyncio.sleep(timeout)

        w.stop()
        await w.wait_shutdown_complete()

    async def run_engine(self, timeout=0.42 if RECORD else 0.02):
        LOG.log(42, "RUNNING ENGINE")
        await self._async_run_workers(timeout)

    def get_gitter(self, logger):
        self.git_counter += 1
        return GitterRecorder(logger, self.cassette_library_dir, self.git_counter)

    async def setup_repo(self, mergify_config=None, test_branches=None, files=None):

        if test_branches is None:
            test_branches = []
        if files is None:
            files = []

        await self.git.configure()
        await self.git.add_cred(
            config.ORG_ADMIN_PERSONAL_TOKEN, "", self.r_o_integration.full_name
        )
        await self.git.add_cred(
            self.FORK_PERSONAL_TOKEN,
            "",
            f"{self.u_fork.login}/{self.r_o_integration.name}",
        )
        await self.git("config", "user.name", f"{config.CONTEXT}-tester")
        await self.git("remote", "add", "main", self.url_main)
        await self.git("remote", "add", "fork", self.url_fork)

        if mergify_config:
            with open(self.git.tmp + "/.mergify.yml", "w") as f:
                f.write(mergify_config)
            await self.git("add", ".mergify.yml")
        else:
            with open(self.git.tmp + "/.gitkeep", "w") as f:
                f.write("repo must not be empty")
            await self.git("add", ".gitkeep")

        if files:
            for name, content in files.items():
                with open(self.git.tmp + "/" + name, "w") as f:
                    f.write(content)
                await self.git("add", name)

        await self.git("commit", "--no-edit", "-m", "initial commit")
        await self.git("branch", "-M", self.master_branch_name)

        for test_branch in test_branches:
            await self.git("branch", test_branch, self.master_branch_name)

        await self.git(
            "push", "--quiet", "main", self.master_branch_name, *test_branches
        )
        await self.git(
            "push", "--quiet", "fork", self.master_branch_name, *test_branches
        )

        self.r_o_admin.edit(default_branch=self.master_branch_name)

    @staticmethod
    def response_filter(response):
        for h in [
            "CF-Cache-Status",
            "CF-RAY",
            "Expect-CT",
            "Report-To",
            "NEL",
            "cf-request-id",
            "Via",
            "X-GitHub-Request-Id",
            "Date",
            "ETag",
            "X-RateLimit-Reset",
            "Expires",
            "Fastly-Request-ID",
            "X-Timer",
            "X-Served-By",
            "Last-Modified",
            "X-RateLimit-Remaining",
            "X-Runtime-rack",
            "Access-Control-Allow-Origin",
            "Access-Control-Expose-Headers",
            "Cache-Control",
            "Content-Security-Policy",
            "Referrer-Policy",
            "Server",
            "Status",
            "Strict-Transport-Security",
            "Vary",
            "X-Content-Type-Options",
            "X-Frame-Options",
            "X-XSS-Protection",
        ]:
            response["headers"].pop(h, None)

        if "body" in response:
            # Urllib3 vcrpy format
            try:
                data = json.loads(response["body"]["string"].decode())
            except ValueError:
                data = None
        else:
            # httpx vcrpy format
            try:
                data = json.loads(response["content"])
            except ValueError:
                data = None

        if data and "token" in data:
            data["token"] = "<TOKEN>"
            if "body" in response:
                # Urllib3 vcrpy format
                response["body"]["string"] = json.dumps(data).encode()
            else:
                # httpx vcrpy format
                response["content"] = json.dumps(data)

        return response

    def get_full_branch_name(self, name):
        return f"{self.BRANCH_PREFIX}/{self._testMethodName}/{name}"

    async def create_pr(
        self,
        base=None,
        files=None,
        two_commits=False,
        base_repo="fork",
        branch=None,
        message=None,
        draft=False,
    ):
        self.pr_counter += 1

        if base is None:
            base = self.master_branch_name

        if not branch:
            branch = f"{base_repo}/pr{self.pr_counter}"
            branch = self.get_full_branch_name(branch)

        title = f"Pull request n{self.pr_counter} from {base_repo}"

        await self.git("checkout", "--quiet", f"{base_repo}/{base}", "-b", branch)
        if files:
            for name, content in files.items():
                directory = name.rpartition("/")[0]
                if directory:
                    try:
                        os.makedirs(self.git.tmp + "/" + directory)
                    except FileExistsError:
                        pass
                with open(self.git.tmp + "/" + name, "w") as f:
                    f.write(content)
                await self.git("add", name)
        else:
            open(self.git.tmp + f"/test{self.pr_counter}", "wb").close()
            await self.git("add", f"test{self.pr_counter}")
        await self.git("commit", "--no-edit", "-m", title)
        if two_commits:
            await self.git(
                "mv", f"test{self.pr_counter}", f"test{self.pr_counter}-moved"
            )
            await self.git("commit", "--no-edit", "-m", f"{title}, moved")
        await self.git("push", "--quiet", base_repo, branch)

        if base_repo == "fork":
            repo = self.r_fork.parent
            login = self.r_fork.owner.login
        else:
            repo = self.r_o_admin
            login = self.r_o_admin.owner.login

        p = repo.create_pull(
            base=base,
            head=f"{login}:{branch}",
            title=title,
            body=message or title,
            draft=draft,
        )

        await self.wait_for("pull_request", {"action": "opened"})

        # NOTE(sileht): We return the same but owned by the main project
        p = self.r_o_integration.get_pull(p.number)
        commits = list(p.get_commits())

        return p, commits

    async def create_status(
        self, pr, context="continuous-integration/fake-ci", state="success"
    ):
        # TODO(sileht): monkey patch PR with this
        self.r_o_admin._requester.requestJsonAndCheck(
            "POST",
            pr.base.repo.url + "/statuses/" + pr.head.sha,
            input={
                "state": state,
                "description": "Your change works",
                "context": context,
            },
            headers={"Accept": "application/vnd.github.machine-man-preview+json"},
        )
        await self.wait_for("status", {"state": state})

    async def create_review(self, pr, commit, event="APPROVE"):
        pr_review = self.r_o_admin.get_pull(pr.number)
        r = pr_review.create_review(commit, "Perfect", event=event)
        await self.wait_for("pull_request_review", {"action": "submitted"})
        return r

    async def create_message(self, pr, message):
        pr_review = self.r_o_admin.get_pull(pr.number)
        comment = pr_review.create_issue_comment(message)
        await self.wait_for("issue_comment", {"action": "created"})
        return comment

    async def add_assignee(self, pr, assignee):
        pr.add_to_assignees(assignee)
        await self.wait_for("pull_request", {"action": "assigned"})

    async def add_label(self, pr, label):
        if label not in self.existing_labels:
            try:
                self.r_o_admin.create_label(label, "000000")
            except pygithub.GithubException as e:
                if e.status != 422:
                    raise

            self.existing_labels.append(label)

        pr.add_to_labels(label)
        await self.wait_for("pull_request", {"action": "labeled"})

    async def remove_label(self, pr, label):
        pr.remove_from_labels(label)
        await self.wait_for("pull_request", {"action": "unlabeled"})

    def branch_protection_unprotect(self, branch):
        return self.r_o_admin._requester.requestJsonAndCheck(
            "DELETE",
            f"{self.r_o_admin.url}/branches/{branch}/protection",
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )

    def branch_protection_protect(self, branch, rule):
        if (
            self.r_o_admin.organization
            and rule["protection"]["required_pull_request_reviews"]
        ):
            rule = copy.deepcopy(rule)
            rule["protection"]["required_pull_request_reviews"][
                "dismissal_restrictions"
            ] = {}

        # NOTE(sileht): Not yet part of the API
        # maybe soon https://github.com/PyGithub/PyGithub/pull/527
        return self.r_o_admin._requester.requestJsonAndCheck(
            "PUT",
            f"{self.r_o_admin.url}/branches/{branch}/protection",
            input=rule["protection"],
            headers={"Accept": "application/vnd.github.luke-cage-preview+json"},
        )
