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
def conv(c):
    out = {"slug": c["slug"], "label": c["label"], "instructions": " ".join(c["instructions"].split())}
    if c.get("also"):
        out["also"] = c["also"]
    if c.get("children"):
        out["children"] = [conv(k) for k in c["children"]]
    return out
cats = [conv(c) for c in src["categories"]]
body += yaml.safe_dump({"categories": cats}, sort_keys=False, width=100, allow_unicode=True)
forms = [{"slug": k["slug"], "label": k["label"], "instructions": " ".join(k["instructions"].split())} for k in src.get("forms", [])]
body += "\n# Forms: the form of a piece, independent of subject. Scores are stored for all of them; thresholds.form decides which show.\n"
body += yaml.safe_dump({"multi_subject_instructions": " ".join(src["multi_subject"].split()), "forms": forms}, sort_keys=False, width=100, allow_unicode=True)
cfg_path.write_text(head + body)
print(f"config.yaml now has {len(cats)} categories, {sum(len(c.get('children', [])) for c in cats)} children")
