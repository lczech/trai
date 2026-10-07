PREFIX ?= $(HOME)/.local
LIBDIR := $(PREFIX)/share/trai
BIN := $(PREFIX)/bin/trai
UUID := trai@lczech.github.io
EXTDIR := $(HOME)/.local/share/gnome-shell/extensions/$(UUID)
UNITDIR := $(or $(XDG_CONFIG_HOME),$(HOME)/.config)/systemd/user
# Seconds between timer ticks; a tick only fetches when the cache is older than refresh_seconds.
INTERVAL ?= 30

.PHONY: test install install-cli install-extension install-timer uninstall dev screenshots

# Remove directory $(1) only if it is absolute and ends in $(2), so an empty or
# odd PREFIX/HOME can never point it elsewhere.
define remove_dir
	@case "$(1)" in /?*"$(2)") [ ! -e "$(1)" ] || rm -r -- "$(1)" ;; \
		*) echo "refusing to remove '$(1)'" >&2; exit 1 ;; esac
endef

test:
	python3 -m unittest

install: install-cli install-extension

install-cli:
	$(call remove_dir,$(LIBDIR),/share/trai)
	mkdir -p "$(LIBDIR)" "$(PREFIX)/bin"
	cp -r trai "$(LIBDIR)/"
	find "$(LIBDIR)" -name __pycache__ -prune -exec rm -r -- {} +
	printf '#!/bin/sh\nexec /usr/bin/python3 -c "import sys; sys.path.insert(0, \\"%s\\"); from trai.cli import run; run()" "$$@"\n' "$(LIBDIR)" > "$(BIN)"
	chmod +x "$(BIN)"
	@echo "installed $(BIN)"

install-extension:
	$(call remove_dir,$(EXTDIR),/extensions/$(UUID))
	mkdir -p "$(EXTDIR)"
	cp -r gnome-extension/. "$(EXTDIR)/"
	glib-compile-schemas "$(EXTDIR)/schemas"
	@scripts/enable-extension.sh $(UUID)

install-timer: install-cli
	@case "$(INTERVAL)" in ''|*[!0-9]*|0) echo "INTERVAL must be a positive number of seconds" >&2; exit 1 ;; esac
	mkdir -p "$(UNITDIR)"
	sed 's|@BIN@|$(BIN)|' systemd/trai-refresh.service > "$(UNITDIR)/trai-refresh.service"
	sed 's|@INTERVAL@|$(INTERVAL)|' systemd/trai-refresh.timer > "$(UNITDIR)/trai-refresh.timer"
	systemctl --user daemon-reload
	systemctl --user enable trai-refresh.timer
	systemctl --user restart trai-refresh.timer
	@echo "trai-refresh.timer ticks every $(INTERVAL) s"

uninstall:
	-gnome-extensions disable $(UUID) 2>/dev/null
	-systemctl --user disable --now trai-refresh.timer 2>/dev/null
	rm -f -- "$(UNITDIR)/trai-refresh.service" "$(UNITDIR)/trai-refresh.timer"
	-systemctl --user daemon-reload 2>/dev/null
	$(call remove_dir,$(LIBDIR),/share/trai)
	$(call remove_dir,$(EXTDIR),/extensions/$(UUID))
	rm -f -- "$(BIN)"

# Nested GNOME Shell in a window, with this checkout's extension.
dev:
	scripts/nested.sh

# Regenerate docs/screenshots/ in a headless GNOME Shell.
screenshots:
	scripts/screenshots/run.sh
