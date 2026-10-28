#!/usr/bin/env python3
"""Assemble a self-contained TokenEconomizer plugin bundle and a marketplace around it.

    python scripts/build_plugin.py [--out dist/marketplace] [--zip]

Why a build step at all. Loaded in place -- `claude --plugin-dir plugin/`, or this repository
added as a local-directory marketplace -- the plugin finds the core package one level up, in the
checkout itself. A marketplace INSTALL is different: Claude Code copies only the plugin directory
into its cache ("files outside the plugin directory aren't copied"), and Codex does the same, so
an installed copy of `plugin/` alone has no `tokeneconomizer` to import. The bundle fixes that by
copying the package (`tokeneconomizer/`, the mechanisms and the plugin runtime) into
`<plugin>/lib/`, which `scripts/te_bootstrap.py` looks in first.

The package is stdlib-only, so copying it is the whole dependency story: the bundle runs on any
Python >= 3.10 with nothing installed. `experiments/`, `scripts/` and the docs are not included.

Layout written to --out (a marketplace root you can `marketplace add`, publish as a branch or a
repository of its own, or zip for `claude --plugin-url`):

    <out>/.claude-plugin/marketplace.json
    <out>/tokeneconomizer/              the plugin, with lib/tokeneconomizer copied in
    <out>/hermes/tokeneconomizer/       the Hermes plugin, self-contained (scripts/, lib/)
"""
import argparse
import json
import os
import shutil
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def build(out):
    out = os.path.abspath(out)
    plugin_out = os.path.join(out, "tokeneconomizer")
    if os.path.exists(plugin_out):
        shutil.rmtree(plugin_out)
    # plugin/lib holds a symlink to the package for an install from a checkout (see
    # te_bootstrap); the bundle replaces it with a real copy.
    shutil.copytree(os.path.join(REPO, "plugin"), plugin_out,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "lib"))
    shutil.copytree(os.path.join(REPO, "tokeneconomizer"),
                    os.path.join(plugin_out, "lib", "tokeneconomizer"), ignore=IGNORE)
    with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"), encoding="utf-8") as fh:
        market = json.load(fh)
    for entry in market.get("plugins", []):
        if entry.get("name") == "tokeneconomizer":
            entry["source"] = "./tokeneconomizer"
    os.makedirs(os.path.join(out, ".claude-plugin"), exist_ok=True)
    with open(os.path.join(out, ".claude-plugin", "marketplace.json"), "w",
              encoding="utf-8") as fh:
        json.dump(market, fh, indent=2)
        fh.write("\n")
    # Hermes loads a plugin directory with its manifest at the top; this one carries the
    # launchers and the core inside it, to copy as `$HERMES_HOME/plugins/tokeneconomizer`.
    hermes_out = os.path.join(out, "hermes", "tokeneconomizer")
    if os.path.exists(hermes_out):
        shutil.rmtree(hermes_out)
    shutil.copytree(os.path.join(plugin_out, "hermes"), hermes_out, ignore=IGNORE)
    for sub in ("scripts", "lib"):
        shutil.copytree(os.path.join(plugin_out, sub), os.path.join(hermes_out, sub),
                        ignore=IGNORE)
    return plugin_out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(REPO, "dist", "marketplace"))
    ap.add_argument("--zip", action="store_true",
                    help="also write <out>/../tokeneconomizer-plugin.zip of the plugin directory")
    args = ap.parse_args(argv)
    plugin_out = build(args.out)
    print(f"plugin bundle: {plugin_out}")
    print(f"marketplace:   {os.path.abspath(args.out)}")
    if args.zip:
        base = os.path.join(os.path.dirname(os.path.abspath(args.out)), "tokeneconomizer-plugin")
        path = shutil.make_archive(base, "zip", root_dir=os.path.dirname(plugin_out),
                                   base_dir=os.path.basename(plugin_out))
        print(f"zip:           {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
