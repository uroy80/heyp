<!--
  Shown at /privacy and /terms when HEYP_MODE=local (the default): a copy of heyp that someone runs for themselves.
  The hosted-mode policy (privacy.md / terms.md) would be untrue here — there is no operator collecting anything —
  so this page explains what the software itself does. Same syntax and values as privacy.md.
-->

# {{page_title}}

*{{site_name}} · self-hosted copy · version {{version}}*

This copy of {{site_name}} runs on a computer or server of whoever started it — not on a public website. Nobody else
operates it, so there's no separate privacy policy or terms of use: the person who runs it is responsible for it, and
the software works as described below.

## What the software does with your files

- Files are uploaded to this copy of {{site_name}} only. They're kept in temporary storage and deleted when you remove
  them (closing the tab asks for that too), at the latest {{ttl_minutes}} minutes after you last used
  them{{#hard_ttl_minutes}} or {{hard_ttl_minutes}} minutes after upload{{/hard_ttl_minutes}}, and whenever it
  restarts.
- It sets no cookies and runs no analytics. Your browser remembers only your light/dark theme choice, under the name
  `heyp-theme`, and only if you press the theme button.
- It sends your files to no one: every tool, including face detection, background removal and upscaling, runs on
  this copy with models stored on it{{#url_tool_enabled}}. The web-page capture tool visits the addresses you give
  it, through a locked-down browser that can reach only public websites{{/url_tool_enabled}}.

## Licence

{{site_name}} is free software under the GNU Affero General Public License v3.0. Source code:
[{{source_tree_url}}]({{source_tree_url}}). Third-party fonts, models and programs keep their own licences, listed on
the [credits page](/credits).

If you run {{site_name}} as a public website, set `HEYP_MODE=hosted` and the operator settings described in
`deploy/DEPLOY.md`: this page is then replaced by a full privacy policy and terms of use.
