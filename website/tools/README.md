# Figures and video for the documentation site

Every figure on the site is drawn from data recorded on real runs, so it can be regenerated when
the code changes. Record how:

1. **Data**: TODO — the command(s) that record the data and where it is written (`DATA/`).
2. **Figures**: `python website/tools/figures.py DATA` → `website/docs/assets/img/*.png`.
3. **Video** (optional): see the `narrated-tutorial-video` skill; keep `narration.json`,
   `storyboard.json` and captured outputs here, the MP4 + poster in `docs/assets/video/`.

Recorded: TODO date, machine.
