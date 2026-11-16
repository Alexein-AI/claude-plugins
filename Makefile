.PHONY: test check

# The hooks' tests: Python's standard library only, no network.
test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests

# The tests, and Claude Code's own check of the plugin and the marketplace.
check: test
	claude plugin validate ./plugins/alexein
	claude plugin validate .
