# Cartridge is a QML plugin, so there is nothing to compile. These targets are
# the whole toolchain: check the code, scan the library, and drive the window
# from the command line.

PLUGIN := audryus.cartridge
ROOT   := $(shell pwd)

.PHONY: test smoke scan check lint-qml restart-shell open config refresh validate help

help:                       ## Show this
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/' | expand -t22

test:                       ## Run the scanner and model tests
	python3 tests/test_scan.py
	python3 tests/test_fixes.py
	node tests/test_model.js

smoke:                      ## Drive the live shell over IPC and check its log
	tests/smoke_shell.sh

check: test lint-qml        ## Everything

lint-qml:                   ## Parse every QML file the way the shell would
	@for file in ui/*.qml; do \
		/usr/lib/qt6/bin/qmllint -I /usr/lib/qt6/qml -I /usr/share/omarchy/shell "$$file" \
			2>&1 | grep -E 'Error|missing-property|Type .* unavailable' && echo "  ^^ $$file"; \
	done; true

validate:                   ## Check manifest.json against Omarchy's schema
	omarchy plugin validate $(ROOT)

scan:                       ## Scan the library now, without the UI
	./bin/cartridge-scan.py

restart-shell:              ## Reload the shell so QML changes take effect
	omarchy-restart-shell

open:                       ## Open the library window
	omarchy-shell $(PLUGIN) open

config:                     ## Open the core-per-console window
	omarchy-shell $(PLUGIN) config

refresh:                    ## Open the library and rescan
	omarchy-shell $(PLUGIN) refresh
