# plugin-linear

A Stavrobot plugin for managing your [Linear](https://linear.app/) workspace —
issues, comments, labels, and projects — through the assistant.

## Tools

- **list_issues** — list issues, optionally filtered by state, project, label or assignee. With no filters this is your open issues.
- **get_issue** — get one issue in full by identifier: description, state, assignee, priority, project, labels, due date, URL, parent and sub-issues, and its most recent comments.
- **search_issues** — full-text search of issues by keyword, across titles, descriptions and comments.
- **list_states** — list the team's workflow states in workflow order, with the type of each.
- **list_labels** — list the workspace's issue labels, including their descriptions and label groups.
- **list_projects** — list projects with their status, lead, target date, progress and external links.
- **create_issue** — create an issue, with optional description, assignee, priority, project, labels and due date.
- **update_issue** — change an issue's fields, or add and remove its labels.
- **add_comment** — add a Markdown comment to an issue.
- **delete_issue** — move an issue to the trash. Destructive and recoverable in Linear for about 30 days; confirm with the user first.
- **create_label** — create a workspace-level issue label.
- **create_project** — create a project, optionally attaching external links.
- **add_project_link** — attach an external URL to an existing project.

## Installation

Install the plugin by asking the bot to install https://github.com/stavrobot/plugin-linear.git.

## Configuration

Set `api_key` to a Linear personal API key:

1. In Linear, open Settings > Security and access > Personal API keys.
2. Create a key and copy it into the plugin's `api_key` configuration value.

The key acts as your Linear user and can read and write your workspace data, so treat it as a secret.

## Updating labels

`update_issue` has no replacing `labels` parameter. Change labels with
`add_labels` and `remove_labels`; the issue's other labels are preserved, so
adding one label keeps the rest.

Those are Linear's native label deltas, and they are strict. Removing a label
that is not currently on the issue is an error, not a silent no-op, and naming
the same label in both `add_labels` and `remove_labels` is an error too. In
either case the whole update fails and nothing else in it is applied. This is
deliberate: the alternative would quietly do the wrong thing.

## What list_issues shows by default

With no filters, `list_issues` returns your open issues — assigned to you,
excluding completed and cancelled ones. Pass `assignee` as `anyone` to see
everyone's open issues instead, or an explicit name or `me`.

## Pagination

No tool paginates. Each list returns a single page and reports in its output
when the limit cut the results short. `list_issues` and `search_issues` default
to 25 results and accept `limit` up to 250; `list_projects` returns up to 50
projects. To see more, raise `limit` or narrow the filters.

## Multi-value parameters

Tool parameters have no array type, so parameters that accept several values —
`labels`, `add_labels`, `remove_labels` and `links` — take a single
comma-separated string, for example `"Bug, P1"`.

## Priority

`priority` takes a word: `none`, `urgent`, `high`, `medium`, or `low`. Not
Linear's integer values.

## Issue identifiers

`get_issue`, `update_issue`, `add_comment` and `delete_issue` take the human
identifier Linear displays, such as `ENG-123`, not a UUID. It is
case-insensitive: `eng-123` works.

The plugin talks to a single Linear team and no tool takes a team parameter;
your account's first team is used, so on a workspace with several teams this
may not be the one you expect.

## Tests

The tests run offline: every network call is replaced with a fake and no
credentials are read. Run the whole suite with:

```sh
for t in tests/test_*.py; do uv run "$t" || exit 1; done
```

Each script prints a PASS/FAIL line per check and exits non-zero if any check
fails.
