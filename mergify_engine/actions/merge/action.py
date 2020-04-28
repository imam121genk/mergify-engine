# -*- encoding: utf-8 -*-
#
# Copyright © 2020 Mergify SAS
# Copyright © 2018 Mehdi Abaakouk <sileht@sileht.net>
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
import itertools
import re

import httpx
import jinja2.exceptions
import jinja2.runtime
import jinja2.sandbox
import jinja2.utils
import voluptuous

from mergify_engine import actions
from mergify_engine import context
from mergify_engine.actions.merge import helpers
from mergify_engine.actions.merge import queue


BRANCH_PROTECTION_FAQ_URL = (
    "https://doc.mergify.io/faq.html#"
    "mergify-is-unable-to-merge-my-pull-request-due-to-"
    "my-branch-protection-settings"
)

MARKDOWN_TITLE_RE = re.compile(r"^#+ ", re.I)
MARKDOWN_COMMIT_MESSAGE_RE = re.compile(r"^#+ Commit Message ?:?\s*$", re.I)


class PullRequestContext(jinja2.runtime.Context):
    """This is a special Context that resolves any attribute first in the "pull request" object.


    This allows to write {{author}} instead of {{pull_request.author}}."""

    _InvalidValue = object()

    def resolve_or_missing(self, key):
        if "pull_request" in self.parent:
            try:
                return getattr(self.parent["pull_request"], key)
            except AttributeError:
                if "pull_request" in self.vars:
                    return getattr(self.vars["pull_request"], key)
                raise

        value = super().resolve_or_missing(key)
        if value == self._InvalidValue:
            return jinja2.utils.missing

    @classmethod
    def inject(cls, env, pull_request):
        """Inject this context into a Jinja Environment."""
        env.globals["pull_request"] = pull_request
        # Set all the value to _InvalidValue as the PullRequestContext will resolve
        # values correctly anyway. We still need to have those entries in
        # the global dict so find_undeclared_variables works correctly.
        env.globals.update(
            dict((k.replace("-", "_"), cls._InvalidValue) for k in pull_request)
        )
        env.context_class = cls


class MergeAction(actions.Action):
    only_once = True

    validator = {
        voluptuous.Required("method", default="merge"): voluptuous.Any(
            "rebase", "merge", "squash"
        ),
        voluptuous.Required("rebase_fallback", default="merge"): voluptuous.Any(
            "merge", "squash", None
        ),
        voluptuous.Required("strict", default=False): voluptuous.Any(bool, "smart"),
        voluptuous.Required("strict_method", default="merge"): voluptuous.Any(
            "rebase", "merge"
        ),
        voluptuous.Required("commit_message", default="default"): voluptuous.Any(
            "default", "title+body"
        ),
    }

    def run(self, ctxt, missing_conditions):
        ctxt.log.info("process merge", config=self.config)

        output = helpers.merge_report(ctxt, self.config["strict"])
        if output:
            if self.config["strict"] == "smart":
                queue.remove_pull(ctxt)
            return output

        if self.config["strict"] and ctxt.is_behind:
            return self._sync_with_base_branch(ctxt)
        else:
            try:
                return self._merge(ctxt)
            finally:
                if self.config["strict"] == "smart":
                    queue.remove_pull(ctxt)

    def cancel(self, ctxt, missing_conditions):
        # We just rebase the pull request, don't cancel it yet if CIs are
        # running. The pull request will be merge if all rules match again.
        # if not we will delete it when we received all CIs termination
        if self.config["strict"] and self._required_statuses_in_progress(
            ctxt, missing_conditions
        ):
            return helpers.get_wait_for_ci_report(ctxt)

        if self.config["strict"] == "smart":
            queue.remove_pull(ctxt)

        return self.cancelled_check_report

    @staticmethod
    def _required_statuses_in_progress(ctxt, missing_conditions):
        # It's closed, it's not going to change
        if ctxt.pull["state"] == "closed":
            return False

        need_look_at_checks = []
        for condition in missing_conditions:
            if condition.attribute_name.startswith("status-"):
                need_look_at_checks.append(condition)
            else:
                # something else does not match anymore
                return False

        if need_look_at_checks:
            if not ctxt.checks:
                return True

            states = [
                state
                for name, state in ctxt.checks.items()
                for cond in need_look_at_checks
                if cond(**{cond.attribute_name: name})
            ]
            if not states:
                return True

            for state in states:
                if state in ("pending", None):
                    return True

        return False

    def _sync_with_base_branch(self, ctxt):
        if not ctxt.pull_base_is_modifiable:
            return (
                "failure",
                "Pull request can't be updated with latest "
                "base branch changes, owner doesn't allow "
                "modification",
                "",
            )
        elif self.config["strict"] == "smart":
            queue.add_pull(ctxt, self.config["strict_method"])
            return (
                None,
                "Base branch will be updated soon",
                "The pull request base branch will "
                "be updated soon, and then merged.",
            )
        else:
            return helpers.update_pull_base_branch(ctxt, self.config["strict_method"])

    @staticmethod
    def _get_commit_message(pull_request, mode="default"):
        if mode == "title+body":
            return pull_request.title, pull_request.body

        if not pull_request.body:
            return

        found = False
        message_lines = []

        for line in pull_request.body.split("\n"):
            if MARKDOWN_COMMIT_MESSAGE_RE.match(line):
                found = True
            elif found and MARKDOWN_TITLE_RE.match(line):
                break
            elif found:
                message_lines.append(line)

        # Remove the first empty lines
        message_lines = list(
            itertools.dropwhile(lambda x: not x.strip(), message_lines)
        )

        if found and message_lines:
            env = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined)
            PullRequestContext.inject(env, pull_request)

            title = message_lines.pop(0)

            # Remove the empty lines between title and message body
            message_lines = list(
                itertools.dropwhile(lambda x: not x.strip(), message_lines)
            )

            return (
                env.from_string(title.strip()).render(),
                env.from_string(
                    "\n".join(line.strip() for line in message_lines)
                ).render(),
            )

    def _merge(self, ctxt):
        if self.config["method"] != "rebase" or ctxt.pull["rebaseable"]:
            method = self.config["method"]
        elif self.config["rebase_fallback"]:
            method = self.config["rebase_fallback"]
        else:
            return (
                "action_required",
                "Automatic rebasing is not possible, manual intervention required",
                "",
            )

        data = {}

        try:
            commit_title_and_message = self._get_commit_message(
                ctxt.pull_request, self.config["commit_message"],
            )
        except jinja2.exceptions.TemplateSyntaxError as tse:
            return (
                "action_required",
                "Invalid commit message",
                f"There is an error in your commit message: {tse.message} at line {tse.lineno}",
            )
        except jinja2.exceptions.TemplateError as te:
            return (
                "action_required",
                "Invalid commit message",
                f"There is an error in your commit message: {te.message}",
            )
        except context.PullRequestAttributeError as e:
            return (
                "action_required",
                "Invalid commit message",
                f"There is an error in your commit message, the following variable is unknown: {e.name}",
            )

        if commit_title_and_message is not None:
            title, message = commit_title_and_message
            if title:
                data["commit_title"] = title
            if message:
                data["commit_message"] = message

        data["sha"] = ctxt.pull["head"]["sha"]
        data["merge_method"] = method

        try:
            ctxt.client.put(f"pulls/{ctxt.pull['number']}/merge", json=data)
        except httpx.HTTPClientSideError as e:  # pragma: no cover
            ctxt.update()
            if ctxt.pull["merged"]:
                ctxt.log.info("merged in the meantime")
            else:
                return self._handle_merge_error(e, ctxt)
        else:
            ctxt.update()
            ctxt.log.info("merged")

        return helpers.merge_report(ctxt, self.config["strict"])

    def _handle_merge_error(self, e, ctxt):
        if "Head branch was modified" in e.message:
            ctxt.log.info(
                "Head branch was modified in the meantime",
                status=e.status_code,
                error_message=e.message,
            )
            return (
                "cancelled",
                "Head branch was modified in the meantime",
                "The head branch was modified, the merge action have been cancelled.",
            )
        elif "Base branch was modified" in e.message:
            # NOTE(sileht): The base branch was modified between pull.is_behind call and
            # here, usually by something not merged by mergify. So we need sync it again
            # with the base branch.
            ctxt.log.info(
                "Base branch was modified in the meantime, retrying",
                status=e.status_code,
                error_message=e.message,
            )
            return self._sync_with_base_branch(ctxt)

        elif e.status_code == 405:
            ctxt.log.info(
                "Waiting for the Branch Protection to be validated",
                status=e.status_code,
                error_message=e.message,
            )
            return (
                None,
                "Waiting for the Branch Protection to be validated",
                "Branch Protection is enabled and is preventing Mergify "
                "to merge the pull request. Mergify will merge when "
                "branch protection settings validate the pull request. "
                f"(detail: {e.message})",
            )
        else:
            message = "Mergify failed to merge the pull request"
            ctxt.log.info(
                "merge fail",
                status=e.status_code,
                mergify_message=message,
                error_message=e.message,
            )
            return ("failure", message, f"GitHub error message: `{e.message}`")