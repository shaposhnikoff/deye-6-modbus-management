.DEFAULT_GOAL := build

SERVER ?=
REMOTE_DIR ?= DEYEMonitoring
REMOTE_UV ?= .local/bin/uv
UV ?= uv
override SERVER := $(value SERVER)
override REMOTE_DIR := $(value REMOTE_DIR)
override REMOTE_UV := $(value REMOTE_UV)
override UV := $(value UV)
export SERVER REMOTE_DIR REMOTE_UV UV

define REMOTE_SCRIPT
set -eu
mode=$$1 dir=$$2 uv=$$3
collector=deye-monitoring.service
mqtt=deye-mqtt.service
fail() { printf '%s\n' "$$1" >&2; exit 1; }
case "$$dir" in /*) ;; *) dir="$$HOME/$$dir" ;; esac
case "$$uv" in /*) ;; *) uv="$$HOME/$$uv" ;; esac
cd "$$dir" || fail 'Deployment directory does not exist.'
[ "$$(pwd -P)" = "$$dir" ] && [ -w . ] || fail 'Invalid deployment directory.'
[ -f pyproject.toml ] && [ ! -L pyproject.toml ] &&
	grep -Fxq 'name = "deye-monitoring"' pyproject.toml || fail 'Wrong deployment project.'
[ ! -L .venv ] && [ -x "$$uv" ] && command -v curl >/dev/null || fail 'Invalid venv or missing uv or curl.'
check_unit() {
	start=$$(/usr/bin/systemctl show --property=ExecStart --value "$$1")
	case "$$start" in
	*"path=$$dir/.venv/bin/dmon ; argv[]=$$dir/.venv/bin/dmon $$2 "*) ;;
	*) fail "Unexpected ExecStart for $$1." ;;
	esac
}
check_unit "$$collector" serve
mqtt_active=no
if /usr/bin/systemctl is-active --quiet "$$mqtt"; then
	check_unit "$$mqtt" publish
	mqtt_active=yes
fi
case "$$mode" in
dry-run) exit 0 ;;
preflight)
	sudo -n -l /usr/bin/systemctl restart "$$collector" >/dev/null ||
		fail 'Noninteractive sudo is required to restart the collector.'
	if [ "$$mqtt_active" = yes ]; then
		sudo -n -l /usr/bin/systemctl try-restart "$$mqtt" >/dev/null ||
			fail 'Noninteractive sudo is required to restart the MQTT service.'
	fi
	exit 0 ;;
esac
UV_PROJECT_ENVIRONMENT=.venv "$$uv" sync --locked --no-dev --extra mqtt --no-editable \
	--reinstall-package deye-monitoring
sudo -n /usr/bin/systemctl restart "$$collector"
if [ "$$mqtt_active" = yes ]; then
	sudo -n /usr/bin/systemctl try-restart "$$mqtt"
fi
attempt=0
until /usr/bin/systemctl is-active --quiet "$$collector" &&
	curl --noproxy '*' --fail --silent --max-time 2 --output /dev/null http://localhost:9101/health; do
	attempt=$$((attempt + 1))
	[ "$$attempt" -lt 20 ] || fail 'Collector health check failed after restart.'
	sleep 1
done
if [ "$$mqtt_active" = yes ]; then
	/usr/bin/systemctl is-active --quiet "$$mqtt" || fail 'MQTT service is inactive after restart.'
fi
printf '%s\n' 'Collector is active and healthy.'
endef
export REMOTE_SCRIPT

.PHONY: build help dry-run install
build:
	@"$$UV" lock --check --offline
	@"$$UV" build

help:
	@printf '%s\n' \
		'make [build]                 Build the Python package locally.' \
		'make dry-run SERVER=user@host  Preview files for an existing Raspberry Pi install.' \
		'make install SERVER=user@host  Update the package and restart existing services.' \
		'Defaults: REMOTE_DIR=DEYEMonitoring REMOTE_UV=.local/bin/uv (relative to SSH home).' \
		'Copies tracked package files from the working tree. Remote files are not pruned.' \
		'Install requires noninteractive sudo for systemctl and health on localhost:9101.'

dry-run install:
	@set -eu; export LC_ALL=C; \
	fail() { printf '%s\n' "$$1" >&2; exit 1; }; \
	case "$$SERVER" in ''|*[!A-Za-z0-9_.@-]*) fail 'Invalid SERVER.' ;; esac; \
	printf '%s\n' "$$SERVER" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]*(@[A-Za-z0-9_][A-Za-z0-9_.-]*)?$$' || fail 'Invalid SERVER.'; \
	for path in "$$REMOTE_DIR" "$$REMOTE_UV"; do \
		case "$$path" in ''|/|*/|*[!A-Za-z0-9_./-]*) fail 'Invalid remote path.' ;; esac; \
		case "/$${path#/}/" in */./*|*/../*|*//*) fail 'Invalid remote path.' ;; esac; \
	done; \
	"$$UV" lock --check --offline; \
	files=$$(git ls-files --error-unmatch -- .python-version README.md pyproject.toml uv.lock ':(glob)src/deye_monitoring/**/*.py'); \
	remote() { \
		printf '%s\n' "$$REMOTE_SCRIPT" | ssh "$$SERVER" "sh -s -- '$$1' '$$REMOTE_DIR' '$$REMOTE_UV'"; \
	}; \
	transfer() { \
		printf '%s\n' "$$files" | rsync -rt --files-from=- --itemize-changes "$$@" ./ "$$SERVER:$$REMOTE_DIR/"; \
	}; \
	case '$@' in \
	dry-run) remote dry-run; transfer --dry-run ;; \
	install) remote preflight; transfer; remote install ;; \
	esac
