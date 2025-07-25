#!/usr/bin/env python3

"""Runner of the 'pull request comments from Clang-Tidy reports' action"""

import argparse
import difflib
import json
import os
import posixpath
import re
import sys
import time
import urllib.parse

import requests
import yaml


def get_diff_line_ranges_per_file(pr_files):
    """Generates and returns a list of line ranges affected by the corresponding patch hunks for
    each file that has been modified by the processed PR"""

    def change_to_line_range(change):
        split_change = change.split(",")
        start = int(split_change[0])

        if len(split_change) > 1:
            size = int(split_change[1])
        else:
            size = 1

        return range(start, start + size)

    result = {}

    for pr_file in pr_files:
        # Not all PR file metadata entries may contain a patch section
        # For example, entries related to removed binary files may not contain it
        if "patch" not in pr_file:
            continue

        file_name = pr_file["filename"]

        # The result is something like ['@@ -101,8 +102,11 @@', '@@ -123,9 +127,7 @@']
        git_line_tags = re.findall(r"^@@ -.*? +.*? @@", pr_file["patch"], re.MULTILINE)

        # We need to get it to a state like this: ['102,11', '127,7']
        changes = [
            tag.replace("@@", "").strip().split()[1].replace("+", "")
            for tag in git_line_tags
        ]

        result[file_name] = []
        for line_range in [change_to_line_range(change) for change in changes]:
            result[file_name].append(line_range)

    return result


def get_pull_request_files(
    github_api_url, github_token, github_api_timeout, repo, pull_request_id
):
    """Generator of GitHub metadata about files modified by the processed PR"""

    # Request a maximum of 100 pages (3000 items)
    for page in range(1, 101):
        result = requests.get(
            f"{github_api_url}/repos/{repo}/pulls/{pull_request_id:d}/files?page={page:d}",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"token {github_token}",
            },
            timeout=github_api_timeout,
        )

        assert result.status_code == requests.codes.ok  # pylint: disable=no-member

        chunk = json.loads(result.text)

        if not chunk:
            break

        yield from chunk


def get_pull_request_comments(
    github_api_url, github_token, github_api_timeout, repo, pull_request_id
):
    """Generator of GitHub metadata about comments to the processed PR"""

    # Request a maximum of 100 pages (3000 items)
    for page in range(1, 101):
        result = requests.get(
            f"{github_api_url}/repos/{repo}/pulls/{pull_request_id:d}/comments?page={page:d}",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"token {github_token}",
            },
            timeout=github_api_timeout,
        )

        assert result.status_code == requests.codes.ok  # pylint: disable=no-member

        chunk = json.loads(result.text)

        if not chunk:
            break

        yield from chunk


def generate_review_comments(
    clang_tidy_fixes, repository_root, diff_line_ranges_per_file, single_comment_markers
):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Generator of the Clang-Tidy review comments"""

    def get_line_by_offset(repository_root, file_path, offset):
        # Clang-Tidy doesn't support multibyte encodings and measures offsets in bytes
        with open(repository_root + file_path, encoding="latin_1") as file:
            source_file = file.read()

        return source_file[:offset].count("\n") + 1

    def validate_warning_applicability(
        diff_line_ranges_per_file, file_path, start_line_num, end_line_num
    ):
        assert end_line_num >= start_line_num

        for line_range in diff_line_ranges_per_file[file_path]:
            assert line_range.step == 1

            if line_range.start <= start_line_num and end_line_num < line_range.stop:
                return True

        return False

    def calculate_replacements_diff(repository_root, file_path, replacements):
        # Apply the replacements in reverse order so that subsequent offsets are not shifted
        replacements.sort(key=lambda item: (-item["Offset"]))

        # Clang-Tidy doesn't support multibyte encodings and measures offsets in bytes
        with open(repository_root + file_path, encoding="latin_1") as file:
            source_file = file.read()

        changed_file = source_file

        for replacement in replacements:
            changed_file = (
                changed_file[: replacement["Offset"]]
                + replacement["ReplacementText"]
                + changed_file[replacement["Offset"] + replacement["Length"] :]
            )

        # Create and return the diff between the original version of the file and the version
        # with the applied replacements
        return difflib.Differ().compare(
            source_file.splitlines(keepends=True),
            changed_file.splitlines(keepends=True),
        )

    def markdown(s):
        md_chars = "\\`*_{}[]<>()#+-.!|"

        def escape_chars(s):
            for ch in md_chars:
                s = s.replace(ch, "\\" + ch)

            return s

        def unescape_chars(s):
            for ch in md_chars:
                s = s.replace("\\" + ch, ch)

            return s

        # Escape markdown characters
        s = escape_chars(s)
        # Decorate quoted symbols as code
        s = re.sub(
            "'([^']*)'", lambda match: "`` " + unescape_chars(match.group(1)) + " ``", s
        )

        return s

    def markdown_url(label, url):
        return f"[{label}]({url})"

    def diagnostic_name_visual(diagnostic_name):
        visual = f"**{markdown(diagnostic_name)}**"

        try:
            first_dash_idx = diagnostic_name.index("-")
        except ValueError:
            return visual

        namespace = urllib.parse.quote_plus(diagnostic_name[:first_dash_idx])
        check_name = urllib.parse.quote_plus(diagnostic_name[first_dash_idx + 1 :])
        return markdown_url(
            visual,
            f"https://clang.llvm.org/extra/clang-tidy/checks/{namespace}/{check_name}.html",
        )

    def generate_single_comment(
        file_path,
        start_line_num,
        end_line_num,
        name,
        message,
        single_comment_marker,
        replacement_text=None,
    ):  # pylint: disable=too-many-arguments,too-many-positional-arguments,line-too-long
        result = {
            "path": file_path,
            "line": end_line_num,
            "side": "RIGHT",
            "body": f"{single_comment_marker} {diagnostic_name_visual(name)} {single_comment_marker}\n"
            + markdown(message),
        }

        if start_line_num != end_line_num:
            result["start_line"] = start_line_num
            result["start_side"] = "RIGHT"

        if replacement_text is not None:
            # Make sure the code suggestion ends with a newline character
            if not replacement_text or replacement_text[-1] != "\n":
                replacement_text += "\n"

            result["body"] += f"\n```suggestion\n{replacement_text}```"

        return result

    for diag in clang_tidy_fixes[  # pylint: disable=too-many-nested-blocks
        "Diagnostics"
    ]:
        # If we have a Clang-Tidy 8 format, then upconvert it to the Clang-Tidy 9+
        if "DiagnosticMessage" not in diag:
            diag["DiagnosticMessage"] = {
                "FileOffset": diag["FileOffset"],
                "FilePath": diag["FilePath"],
                "Message": diag["Message"],
                "Replacements": diag["Replacements"],
            }

        diag_message = diag["DiagnosticMessage"]

        # Normalize paths
        diag_message["FilePath"] = posixpath.normpath(
            diag_message["FilePath"].replace(repository_root, "")
        )
        for replacement in diag_message["Replacements"]:
            replacement["FilePath"] = posixpath.normpath(
                replacement["FilePath"].replace(repository_root, "")
            )

        diag_name = diag["DiagnosticName"]
        diag_message_msg = diag_message["Message"]

        if diag["Level"] in single_comment_markers:
            single_comment_marker = single_comment_markers[diag["Level"]]
        else:
            single_comment_marker = single_comment_markers["fallback"]

        if not diag_message["Replacements"]:
            file_path = diag_message["FilePath"]
            offset = diag_message["FileOffset"]

            if file_path not in diff_line_ranges_per_file:
                print(
                    f"'{diag_name}' for {file_path} does not apply to the files changed in this PR"
                )
                continue

            line_num = get_line_by_offset(repository_root, file_path, offset)

            print(f"Processing '{diag_name}' at line {line_num:d} of {file_path}...")

            if validate_warning_applicability(
                diff_line_ranges_per_file, file_path, line_num, line_num
            ):
                yield generate_single_comment(
                    file_path,
                    line_num,
                    line_num,
                    diag_name,
                    diag_message_msg,
                    single_comment_marker=single_comment_marker,
                )
            else:
                print("This warning does not apply to the lines changed in this PR")
        else:
            diag_message_replacements = diag_message["Replacements"]

            for file_path in {item["FilePath"] for item in diag_message_replacements}:
                if file_path not in diff_line_ranges_per_file:
                    # pylint: disable=line-too-long
                    print(
                        f"'{diag_name}' for {file_path} does not apply to the files changed in this PR"
                    )
                    continue

                line_num = 1
                start_line_num = None
                end_line_num = None
                replacement_text = None

                for line in calculate_replacements_diff(
                    repository_root,
                    file_path,
                    [
                        item
                        for item in diag_message_replacements
                        if item["FilePath"] == file_path
                    ],
                ):
                    # The comment line in the diff, ignore it
                    if line.startswith("? "):
                        continue

                    # A string belonging only to the original version is the beginning or
                    # continuation of the section of the file that should be replaced
                    if line.startswith("- "):
                        if start_line_num is None:
                            assert end_line_num is None

                            start_line_num = line_num
                            end_line_num = line_num
                        else:
                            assert end_line_num is not None

                            end_line_num = line_num

                        if replacement_text is None:
                            replacement_text = ""

                        line_num += 1
                    # A string belonging only to the modified version is part of the
                    # replacement text
                    elif line.startswith("+ "):
                        if replacement_text is None:
                            replacement_text = line[2:]
                        else:
                            replacement_text += line[2:]
                    # A string belonging to both original and modified versions is the
                    # end of the section to replace
                    elif line.startswith("  "):
                        if replacement_text is not None:
                            # If there is a replacement text, but there is no information about
                            # the section to replace, then this is not a replacement, but a pure
                            # addition of text. Add the current line to the end of the replacement
                            # text and "replace" it with the replacement text.
                            if start_line_num is None:
                                assert end_line_num is None

                                start_line_num = line_num
                                end_line_num = line_num
                                replacement_text += line[2:]
                            else:
                                assert end_line_num is not None

                            print(
                                # pylint: disable=line-too-long
                                f"Processing '{diag_name}' at lines {start_line_num:d}-{end_line_num:d} of {file_path}..."
                            )

                            if validate_warning_applicability(
                                diff_line_ranges_per_file,
                                file_path,
                                start_line_num,
                                end_line_num,
                            ):
                                yield generate_single_comment(
                                    file_path,
                                    start_line_num,
                                    end_line_num,
                                    diag_name,
                                    diag_message_msg,
                                    single_comment_marker=single_comment_marker,
                                    replacement_text=replacement_text,
                                )
                            else:
                                print(
                                    "This warning does not apply to the lines changed in this PR"
                                )

                            start_line_num = None
                            end_line_num = None
                            replacement_text = None

                        line_num += 1
                    # Unknown prefix, this should not happen
                    else:
                        assert False, "Please report this to the repository maintainer"

                # The end of the file is reached, but there is a section to replace
                if replacement_text is not None:
                    # Pure addition of text to the end of the file is not currently supported. If
                    # you have an example of a Clang-Tidy replacement of this kind, please contact
                    # the repository maintainer.
                    assert (
                        start_line_num is not None and end_line_num is not None
                    ), "Please report this to the repository maintainer"

                    print(
                        # pylint: disable=line-too-long
                        f"Processing '{diag_name}' at lines {start_line_num:d}-{end_line_num:d} of {file_path}..."
                    )

                    if validate_warning_applicability(
                        diff_line_ranges_per_file,
                        file_path,
                        start_line_num,
                        end_line_num,
                    ):
                        yield generate_single_comment(
                            file_path,
                            start_line_num,
                            end_line_num,
                            diag_name,
                            diag_message_msg,
                            single_comment_marker=single_comment_marker,
                            replacement_text=replacement_text,
                        )
                    else:
                        print(
                            "This warning does not apply to the lines changed in this PR"
                        )


def post_review_comments(
    github_api_url,
    github_token,
    github_api_timeout,
    repo,
    pull_request_id,
    warning_comment_prefix,
    review_event,
    review_comments,
    suggestions_per_comment,
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Sending the Clang-Tidy review comments to GitHub"""

    def split_into_chunks(lst, n):
        # Copied from: https://stackoverflow.com/a/312464
        """Yield successive n-sized chunks from lst."""
        for i in range(0, len(lst), n):
            yield lst[i : i + n]

    # Split the comments in chunks to avoid overloading the server
    # and getting 502 server errors as a response for large reviews
    review_comments = list(split_into_chunks(review_comments, suggestions_per_comment))

    total_reviews = len(review_comments)
    current_review = 1

    for comments_chunk in review_comments:
        warning_comment = (
            warning_comment_prefix + f" ({current_review:d}/{total_reviews:d})"
        )
        current_review += 1

        result = requests.post(
            f"{github_api_url}/repos/{repo}/pulls/{pull_request_id:d}/reviews",
            json={
                "body": warning_comment,
                "event": review_event,
                "comments": comments_chunk,
            },
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"token {github_token}",
            },
            timeout=github_api_timeout,
        )

        # Ignore bad gateway errors (false negatives?)
        assert result.status_code in (
            requests.codes.ok,  # pylint: disable=no-member
            requests.codes.bad_gateway,  # pylint: disable=no-member
        ), f"Unexpected status code: {result.status_code:d}"

        # Avoid triggering abuse detection
        time.sleep(10)


def dismiss_change_requests(
    github_api_url,
    github_token,
    github_api_timeout,
    repo,
    pull_request_id,
    warning_comment_prefix,
    auto_resolve_conversations,
    single_comment_markers,
):  # pylint: disable=too-many-arguments,too-many-positional-arguments
    """Dismissing stale Clang-Tidy requests for changes"""

    print("Checking if there are any stale requests for changes to dismiss...")

    result = requests.get(
        f"{github_api_url}/repos/{repo}/pulls/{pull_request_id:d}/reviews",
        headers={
            "Accept": "application/vnd.github.v3+json",
            "Authorization": f"token {github_token}",
        },
        timeout=github_api_timeout,
    )

    assert result.status_code == requests.codes.ok  # pylint: disable=no-member

    reviews = json.loads(result.text)

    # Dismiss only our own reviews
    reviews_to_dismiss = [
        review["id"]
        for review in reviews
        if review["state"] == "CHANGES_REQUESTED"
        and warning_comment_prefix in review["body"]
        and review["user"]["login"] == "github-actions[bot]"
    ]

    for review_id in reviews_to_dismiss:
        print(f"Dismissing review {review_id:d}")

        result = requests.put(
            # pylint: disable=line-too-long
            f"{github_api_url}/repos/{repo}/pulls/{pull_request_id:d}/reviews/{review_id:d}/dismissals",
            headers={
                "Accept": "application/vnd.github.v3+json",
                "Authorization": f"token {github_token}",
            },
            json={
                "message": "No Clang-Tidy warnings found so I assume my comments were addressed",
                "event": "DISMISS",
            },
            timeout=github_api_timeout,
        )

        assert result.status_code == requests.codes.ok  # pylint: disable=no-member

        # Avoid triggering abuse detection
        time.sleep(10)

    if auto_resolve_conversations:
        resolve_conversations(
            github_token=github_token,
            repo=repo,
            pull_request_id=pull_request_id,
            github_api_timeout=github_api_timeout,
            single_comment_markers=single_comment_markers,
        )


def conversation_threads_to_close(
    repo, pr_number, github_token, github_api_timeout, single_comment_markers
):
    """Generator of unresolved conversation threads to close

    Uses the GitHub GraphQL API to get conversation threads for the given PR.
    Then filters for unresolved threads and those that have been created by the action.
    """

    repo_owner, repo_name = repo.split("/")
    query = """
    query {
      repository(owner: "%s", name: "%s") {
        pullRequest(number: %d) {
          id
          reviewThreads(last: 100) {
            nodes {
              id
              isResolved
              comments(first: 1) {
                nodes {
                  id
                  body
                  author {
                    login
                  }
                }
              }
            }
          }
        }
      }
    }
    """ % (
        repo_owner,
        repo_name,
        pr_number,
    )

    response = requests.post(
        "https://api.github.com/graphql",
        json={"query": query},
        headers={"Authorization": "Bearer " + github_token},
        timeout=github_api_timeout,
    )

    if response.status_code != 200:
        print(
            f"::error::getting unresolved conversation threads: {response.status_code}"
        )
        raise RuntimeError("Failed to get unresolved conversation threads.")

    data = response.json()

    # list of regexes that matches comments with repeated marker emojis
    marker_matches = []
    for single_comment_marker in single_comment_markers.values():
        single_comment_marker = re.escape(single_comment_marker)
        comment_matcher = re.compile(
            f"^{single_comment_marker}.*{single_comment_marker}.*", re.DOTALL
        )
        marker_matches.append(comment_matcher)

    # Iterate through review threads
    for thread in data["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]:
        for comment in thread["comments"]["nodes"]:
            if (
                comment["id"]
                and thread["isResolved"] is False
                # this actor here is somehow different from `github-actions[bot]`
                # which we get through the Rest API
                and comment["author"]["login"] == "github-actions"
                and any(
                    matcher.match(comment["body"].strip()) for matcher in marker_matches
                )
            ):
                yield thread
                break


def close_conversation(thread_id, github_token, github_api_timeout):
    """Close a conversation thread using the GitHub GraphQL API"""
    mutation = (
        """
    mutation {
      resolveReviewThread(input: {threadId: "%s", clientMutationId: "github-actions"}) {
        thread {
          id
        }
      }
    }
    """
        % thread_id
    )

    print(f"::debug::Closing conversation {thread_id}...")
    response = requests.post(
        "https://api.github.com/graphql",
        json={"query": mutation},
        headers={"Authorization": "Bearer " + github_token},
        timeout=github_api_timeout,
    )

    def _print_error_and_raise(msg):
        print(
            f"::error::{msg}"
            "::error:: Failed to close conversation. See log for details and "
            "https://github.com/platisd/clang-tidy-pr-comments/blob/master/README.md for help"
        )
        raise RuntimeError("Failed to close conversation.")

    if response.status_code != 200:
        _print_error_and_raise(f"GraphQL request failed: {response.status_code}")

    if "errors" in response.json():
        error_msg = response.json()["errors"][0]["message"]
        _print_error_and_raise(
            "Closing conversations requires `contents: write` permission."
            if "Resource not accessible by integration" in error_msg
            else f"Closing conversation query failed: {error_msg}"
        )
    print("Conversation closed successfully.")


def resolve_conversations(
    github_token, repo, pull_request_id, github_api_timeout, single_comment_markers
):
    """Resolving stale conversations"""
    for thread in conversation_threads_to_close(
        repo, pull_request_id, github_token, github_api_timeout, single_comment_markers
    ):
        close_conversation(
            thread_id=thread["id"],
            github_token=github_token,
            github_api_timeout=github_api_timeout,
        )


def reorder_diagnostics(diags):
    """
    order diagnostics by level: first error, then warning, then remark
    """
    errors = [d for d in diags if d["Level"] == "Error"]
    warnings = [d for d in diags if d["Level"] == "Warning"]
    remarks = [d for d in diags if d["Level"] == "Remark"]
    others = [d for d in diags if d["Level"] not in {"Error", "Warning", "Remark"}]

    if others:
        print(
            "WARNING: some fixes have an unexpected Level (e.g. not Error, Warning, Remark)"
        )

    return errors + warnings + remarks + others


def main():
    """Entry point"""

    parser = argparse.ArgumentParser(
        description="Runner of the 'pull request comments from Clang-Tidy reports' action"
    )
    parser.add_argument(
        "--clang-tidy-fixes",
        type=str,
        required=True,
        help="Path to the Clang-Tidy fixes YAML",
    )
    parser.add_argument(
        "--pull-request-id",
        type=int,
        required=True,
        help="Pull request ID",
    )
    parser.add_argument(
        "--repository",
        type=str,
        required=True,
        help="Name of the repository containing the code",
    )
    parser.add_argument(
        "--repository-root",
        type=str,
        required=True,
        help="Path to the root of the repository containing the code",
    )
    parser.add_argument(
        "--request-changes",
        type=str,
        required=True,
        help="If 'true', then request changes if there are warnings, otherwise leave a comment",
    )
    parser.add_argument(
        "--suggestions-per-comment",
        type=int,
        required=True,
        help="Number of suggestions per comment",
    )
    parser.add_argument(
        "--auto-resolve-conversations",
        type=str,
        required=True,
        help="If 'true', then close any discussions opened by the Action",
    )

    args = parser.parse_args()

    # The GitHub API token is sensitive information, pass it through the environment
    github_token = os.environ.get("INPUT_GITHUB_TOKEN")

    github_api_url = os.environ.get("GITHUB_API_URL")
    github_api_timeout = 10

    warning_comment_prefix = (
        ":warning: `Clang-Tidy` found issue(s) with the introduced code"
    )
    single_comment_markers = {
        "Error": ":x:",
        "Warning": ":warning:",
        "Remark": ":speech_balloon:",
        "fallback": ":grey_question:",
    }

    diff_line_ranges_per_file = get_diff_line_ranges_per_file(
        get_pull_request_files(
            github_api_url,
            github_token,
            github_api_timeout,
            args.repository,
            args.pull_request_id,
        )
    )

    if os.path.isfile(args.clang_tidy_fixes):
        with open(args.clang_tidy_fixes, encoding="utf_8") as file:
            clang_tidy_fixes = yaml.safe_load(file)
    else:
        print(
            f"Could not find the clang-tidy fixes file '{args.clang_tidy_fixes}',"
            " it is assumed that it was not generated"
        )
        clang_tidy_fixes = None

    if (
        clang_tidy_fixes is None
        or "Diagnostics" not in clang_tidy_fixes
        or not clang_tidy_fixes["Diagnostics"]
    ):
        print("No warnings found by Clang-Tidy")
        dismiss_change_requests(
            github_api_url,
            github_token,
            github_api_timeout,
            args.repository,
            args.pull_request_id,
            warning_comment_prefix=warning_comment_prefix,
            auto_resolve_conversations=args.auto_resolve_conversations == "true",
            single_comment_markers=single_comment_markers,
        )
        return 0

    clang_tidy_fixes["Diagnostics"] = reorder_diagnostics(
        clang_tidy_fixes["Diagnostics"]
    )

    review_comments = list(
        generate_review_comments(
            clang_tidy_fixes,
            args.repository_root + "/",
            diff_line_ranges_per_file,
            single_comment_markers=single_comment_markers,
        )
    )

    existing_pull_request_comments = list(
        get_pull_request_comments(
            github_api_url,
            github_token,
            github_api_timeout,
            args.repository,
            args.pull_request_id,
        )
    )

    # Exclude already posted comments
    for comment in existing_pull_request_comments:
        review_comments = list(
            filter(
                lambda review_comment: not (
                    review_comment["path"]
                    == comment["path"]  # pylint: disable=cell-var-from-loop
                    and review_comment["line"]
                    == comment["line"]  # pylint: disable=cell-var-from-loop
                    and review_comment["side"]
                    == comment["side"]  # pylint: disable=cell-var-from-loop
                    and review_comment["body"]
                    == comment["body"]  # pylint: disable=cell-var-from-loop
                ),
                review_comments,
            )
        )

    if not review_comments:
        print("No new warnings found by Clang-Tidy")
        return 0

    print(f"Clang-Tidy found {len(review_comments):d} new warning(s)")

    post_review_comments(
        github_api_url,
        github_token,
        github_api_timeout,
        args.repository,
        args.pull_request_id,
        warning_comment_prefix,
        "REQUEST_CHANGES" if args.request_changes == "true" else "COMMENT",
        review_comments,
        args.suggestions_per_comment,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
