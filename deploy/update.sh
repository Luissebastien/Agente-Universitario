#!/bin/sh
# One-command update of a deployed Agente U host:
#
#     sudo /opt/agente-u/deploy/update.sh
#
# Stops the service, fast-forwards to the pushed revision, reinstalls only
# what actually changed, runs the test suite on this host, and starts the
# service again.
#
# If the tests fail the service is deliberately LEFT STOPPED and the exact
# rollback command is printed. Starting code that fails its own tests on the
# target host would be worse than a scheduler that is idle for a while: it
# runs every 6 hours and never replays missed cycles, so being down briefly
# costs nothing, while a bad cycle writes to the database.
#
# The whole script lives inside main() on purpose: `git merge` rewrites this
# very file, and /bin/sh reads scripts incrementally. Wrapping it in a
# function forces the shell to parse everything before any of it runs.

set -eu

SERVICE=agente-u
REPO=/opt/agente-u
VENV=$REPO/.venv/bin/python
CONFIG=/etc/agente-u/scheduler.toml
TESTLOG=/var/tmp/agente-u-update-tests.log

main() {
    if [ "$(id -u)" -ne 0 ]; then
        echo "Run it with sudo: sudo $REPO/deploy/update.sh" >&2
        exit 1
    fi

    cd "$REPO"
    before=$(git rev-parse HEAD)

    git fetch --quiet origin
    target=$(git rev-parse '@{u}')

    if [ "$before" = "$target" ]; then
        echo "Already at $(git rev-parse --short HEAD); nothing to update."
        exit 0
    fi

    echo "=== Updating $(git rev-parse --short "$before") -> $(git rev-parse --short "$target")"
    git --no-pager log --oneline "$before..$target"
    echo

    systemctl stop "$SERVICE"

    # --ff-only, not pull: refuse to merge if this host has diverged, rather
    # than inventing a merge commit on a server nobody develops on.
    git merge --ff-only "$target"

    changed=$(git diff --name-only "$before" HEAD)

    if echo "$changed" | grep -qE '^(pyproject\.toml|constraints\.txt)$'; then
        echo "=== Dependencies changed: reinstalling"
        # TMPDIR: /tmp is a RAM-backed tmpfs and large wheels do not fit.
        env TMPDIR=/var/tmp "$VENV" -m pip install --no-cache-dir \
            -c constraints.txt -e .
    fi

    if echo "$changed" | grep -q '^deploy/scheduler\.toml$'; then
        echo "=== VPS configuration changed: reinstalling"
        install -m 644 deploy/scheduler.toml "$CONFIG"
    fi

    if echo "$changed" | grep -qE '^deploy/.*\.(service|timer)$'; then
        echo "=== systemd units changed: reinstalling"
        install -m 644 deploy/agente-u.service        /etc/systemd/system/
        install -m 644 deploy/agente-u-backup.service /etc/systemd/system/
        install -m 644 deploy/agente-u-backup.timer   /etc/systemd/system/
        systemctl daemon-reload
    fi

    chmod 755 deploy/backup.sh deploy/update.sh

    echo "=== Running the test suite on this host"
    if ! "$VENV" -m unittest discover -s tests -t . >"$TESTLOG" 2>&1; then
        echo
        tail -n 25 "$TESTLOG" >&2
        cat >&2 <<EOF

!!! UPDATE ABORTED: the test suite failed on this host.
!!! $SERVICE is NOT running. Full output: $TESTLOG
!!!
!!! To return to the previous version and start the service:
!!!
!!!     sudo git -C $REPO checkout $before && sudo systemctl start $SERVICE
!!!
EOF
        exit 1
    fi
    tail -n 3 "$TESTLOG"

    systemctl start "$SERVICE"
    sleep 2
    systemctl --no-pager --lines=0 status "$SERVICE" || true

    echo
    echo "=== Updated to $(git rev-parse --short HEAD)."
    echo "    Logs:   journalctl -u $SERVICE -f"
    echo "    Status: sudo -u $SERVICE $VENV -m scheduler --config $CONFIG --status"
}

main "$@"
