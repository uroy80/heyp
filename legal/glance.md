<!--
  "Privacy at a glance" — the boxed fact box at the top of /privacy (included with {{> glance}}).
  Same syntax and values as privacy.md. Keep it to a handful of lines; every line must also be true.
-->
<aside class="factbox" aria-labelledby="glance-title">

<h2 id="glance-title" class="factbox-title">Privacy at a glance</h2>

- **No account, no cookies, no analytics, no ads.** Nothing tracks you here.
- **Your files are processed on our server, then deleted** — when you remove them or close the tab, and never more
  than **{{ttl_minutes}} minutes** after you last used them{{#hard_ttl_minutes}} (**{{hard_ttl_minutes}} minutes** after
  upload at most){{/hard_ttl_minutes}}.
- **Nobody looks at your files**, they never go to an AI service, and they're never used to train AI.
- **Our server keeps a basic access log** ({{#log_ip_truncated}}shortened {{/log_ip_truncated}}IP address,
  time, page) for {{log_retention_days}} days, to keep the service safe. Never your file names or contents.
- **Open source** (AGPL-3.0) — [read the code]({{source_tree_url}}) behind this page.

Questions: {{contact_email}} · [Full privacy policy](/privacy) · [Terms](/terms)

</aside>
