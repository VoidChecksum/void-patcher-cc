# systemd — vpcc autoheal

User-level timer. No root. Runs `vpcc autoheal --quiet` every 6h with a 30min
jitter. `autoheal` is a no-op when Claude Code's sha hasn't changed, so the
cost of running often is essentially zero.

## install

```bash
install -d ~/.config/systemd/user
install -m 0644 vpcc-autoheal.service ~/.config/systemd/user/
install -m 0644 vpcc-autoheal.timer   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now vpcc-autoheal.timer
```

## inspect

```bash
systemctl --user list-timers vpcc-autoheal.timer
systemctl --user status vpcc-autoheal.service
journalctl --user -u vpcc-autoheal.service -n 50
```

## remove

```bash
systemctl --user disable --now vpcc-autoheal.timer
rm ~/.config/systemd/user/vpcc-autoheal.{service,timer}
systemctl --user daemon-reload
```
