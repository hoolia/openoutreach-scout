# Review guidance

- READ_ONLY stays the default and wins over SEND_ENABLED. No change may add a LinkedIn
  write that `guard.SEND_PATHS` or the network block does not cover; a new upstream pin
  must pass `tests/test_guard.py` unchanged or with each newly reviewed read justified.
- The pacing defaults are the hard bounds of the design. No change may raise a bound in
  `config.HARD`; the Helm chart's `values.schema.json` holds the same bounds.
- The scout never types LinkedIn credentials and never retries a login.
- The Chatwoot client has no message or note method; its only write is the label list.
- Upstream code is fetched by commit and SHA-256 only (`upstream.env`); the Containerfile
  installs Python dependencies from the hashed lock only.
- Logs, alerts and errors never carry a token, password or cookie.
