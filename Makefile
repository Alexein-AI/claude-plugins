.PHONY: test check sync

# The hooks' tests: Python's standard library only, no network.
test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests

# The tests, and Claude Code's own check of the plugin and the marketplace.
check: test
	claude plugin validate ./plugins/alexein
	claude plugin validate .

# The other agents' plugins carry a copy of the hooks script: copy it into those checkouts when they sit beside this
# one (copilot-plugin, cursor-plugin, antigravity-plugin).
SCRIPT := plugins/alexein/scripts/alexein_hooks.py
sync:
	@for repo in copilot-plugin cursor-plugin antigravity-plugin; do \
	  if [ -d ../$$repo ]; then mkdir -p ../$$repo/scripts && cp $(SCRIPT) ../$$repo/scripts/ && echo "copied to ../$$repo"; fi; \
	done
