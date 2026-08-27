# omavoice — development helpers.
#
# Users do not need this file: they install with `omarchy plugin add` and run
# scripts/setup.sh. This is for working on the plugin from a source checkout
# that lives somewhere other than the plugins directory.
#
# The plugin cannot be symlinked into place — Omarchy's validator rejects
# symlinks anywhere inside a plugin folder — so `watch` copies on every save.
# The shell has its own inotify watcher and reloads QML by itself.

PLUGIN_ID  := io.github.baranskyi.omavoice
PLUGIN_DST := $(HOME)/.config/omarchy/plugins/$(PLUGIN_ID)
VENV       := $(HOME)/.local/share/omavoice/venv
PYTHON     := $(VENV)/bin/python

.PHONY: help install sync validate lint watch logs status uninstall

help:
	@echo "make install    — copy the plugin into place, then run setup.sh"
	@echo "make sync       — copy the plugin into place only"
	@echo "make validate   — Omarchy manifest check + symlink check + qmllint"
	@echo "make watch      — re-copy on every save"
	@echo "make logs       — follow the daemon journal"
	@echo "make status     — ask the daemon what it is doing"
	@echo "make uninstall  — remove the service, the venv and the plugin"

sync:
	@mkdir -p $(PLUGIN_DST)
	@rsync -a --delete --exclude '.git' --exclude '__pycache__' ./ $(PLUGIN_DST)/
	@echo "plugin -> $(PLUGIN_DST)"

install: sync
	@bash $(PLUGIN_DST)/scripts/setup.sh
	@omarchy-shell -q shell rescanPlugins || true

validate:
	@omarchy plugin validate $(PLUGIN_DST)
	@test -z "$$(find . -type l -not -path './.git/*')" \
		&& echo "no symlinks ok" \
		|| { echo "symlinks found — Omarchy will reject the plugin:"; \
		     find . -type l -not -path './.git/*'; exit 1; }
	@qmllint -I $(OMARCHY_PATH)/shell $(PLUGIN_DST)/*.qml || true

lint:
	@$(PYTHON) -m compileall -q daemon/omavoice && echo "python ok"

watch:
	@echo "watching — Ctrl-C to stop"
	@while inotifywait -qq -r -e close_write,create,delete,move --exclude '\.git' . ; do \
		$(MAKE) --no-print-directory sync; \
	done

logs:
	@journalctl --user -u omavoice -f -n 50

status:
	@bin/omavoice-ctl status

uninstall:
	@bash scripts/uninstall.sh
	@rm -rf $(PLUGIN_DST)
