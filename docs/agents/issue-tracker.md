# Issue tracker: GitHub

Tasks and specs live in https://github.com/alkselsv/ActionFlow/issues.
Use the gh CLI with --repo alkselsv/ActionFlow.

- Before creating an issue, verify the target with gh repo view.
- Read tickets with gh issue view; list them with gh issue list.
- Create tickets with gh issue create. Pass multiline bodies via --body-file.
- Apply or remove labels with gh issue edit.
- Use native GitHub sub-issues and blocking dependencies when available.
  Otherwise, record "Part of #N" and "Blocked by: #N" in the ticket body.
- Before closing an issue, post a comment explaining the resolution.

When a skill says "publish to the issue tracker", create a GitHub issue.
When it says "fetch the relevant ticket", read the GitHub issue.

## Pull requests as a triage surface

**PRs as a request surface: no.**
