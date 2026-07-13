# Developer entry points. See CLAUDE.md → Testing.

.PHONY: test smoke smoke-graphics smoke-avatar validate-fixtures

# Fast unit tests (no network, no ffmpeg renders; ~1s).
test:
	venv/bin/python -m pytest tests/ -q

# Sanity-check the committed fixture jobs against the Phase-2 gate.
validate-fixtures:
	venv/bin/python scripts/validate_job.py tests/fixtures/smoke-basic.job.json
	venv/bin/python scripts/validate_job.py tests/fixtures/smoke-graphics.job.json
	venv/bin/python scripts/validate_job.py tests/fixtures/smoke-avatar.job.json

# End-to-end pipeline smoke on the basic fixture (needs network: TTS +
# stock-footage APIs; ~ a few minutes). SKIP_WHISPER + REVIDEO_ENABLED=0
# give the fastest loop; graphics fall back to footage.
smoke:
	rm -rf "storage/tasks/smoke-basic"
	mkdir -p "storage/tasks/smoke-basic"
	cp tests/fixtures/smoke-basic.job.json "storage/tasks/smoke-basic/job.json"
	SKIP_WHISPER=1 REVIDEO_ENABLED=0 venv/bin/python cli.py --job "storage/tasks/smoke-basic/job.json"

# Avatar-block plumbing smoke: sentences 0–1 form an avatar block rendered as
# a dry-run slate (no RunPod call, no key needed). AVATAR_* env overrides let
# the fixture run regardless of what config.toml says.
smoke-avatar:
	rm -rf "storage/tasks/smoke-avatar"
	mkdir -p "storage/tasks/smoke-avatar"
	cp tests/fixtures/smoke-avatar.job.json "storage/tasks/smoke-avatar/job.json"
	SKIP_WHISPER=1 REVIDEO_ENABLED=0 AVATAR_ENABLED=1 AVATAR_DRY_RUN=1 \
		venv/bin/python cli.py --job "storage/tasks/smoke-avatar/job.json"

# Same but exercises named-track + narrated graphics (Revideo enabled).
smoke-graphics:
	rm -rf "storage/tasks/smoke-graphics"
	mkdir -p "storage/tasks/smoke-graphics"
	cp tests/fixtures/smoke-graphics.job.json "storage/tasks/smoke-graphics/job.json"
	SKIP_WHISPER=1 venv/bin/python cli.py --job "storage/tasks/smoke-graphics/job.json"
