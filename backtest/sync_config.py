#!/usr/bin/env python3
"""Regenerate the category block of ../TDM-jev-ClaudeProposalCurrent/config.yaml from categories.yaml,
so the backtest and the pipeline always use the same wording. Everything above '# Gate question' is kept."""
import yaml
from pathlib import Path
here = Path(__file__).resolve().parent
cfg_path = here.parent / "TDM-jev-ClaudeProposalCurrent" / "config.yaml"
src = yaml.safe_load((here / "categories.yaml").read_text())
cfg = cfg_path.read_text()
head = cfg.split("# Gate question")[0]
tail_comment = cfg[cfg.index("# One Noul"):cfg.index("categories:\n")]
body = ("# Gate question: is the item about motorcycles at all? Policy: scooters, mopeds, e-bikes, cars,\n"
        "# bicycles and snowmobiles are off-topic for now. Asked in the same call as the categories.\n"
        "on_topic_instructions: >\n  " + " ".join(src["on_topic"].split()) + "\n\n" + tail_comment)
cats = [{"slug": c["slug"], "label": c["label"], "instructions": " ".join(c["instructions"].split())} for c in src["categories"]]
body += yaml.safe_dump({"categories": cats}, sort_keys=False, width=100, allow_unicode=True)
cfg_path.write_text(head + body)
print(f"config.yaml now has {len(cats)} categories")
