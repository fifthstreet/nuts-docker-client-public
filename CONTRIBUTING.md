# Contributing

Bug reports, fixes and small improvements are welcome.

- **Bugs:** open an issue with what you expected, what happened, your `docker compose ps` output and the relevant log lines. Remove keys and passwords first.
- **Changes:** keep it small and focused, open an issue first for anything big.
- The app is one Python file using only the standard library; please keep it that way.
- Run the tests (no dependencies, about 40 seconds): `python3 -m unittest discover -s tests`. Add a test for what you change.
- A shutdown tool should be boring and safe: nothing may power a host off unless the switch is ON, and new behaviour should fail toward "do not shut down" except where a rule says otherwise.
- Update the README or `docs/` if behaviour or settings change, and add a line to `CHANGELOG.md`.
