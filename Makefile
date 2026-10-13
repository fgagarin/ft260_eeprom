# Release: make release [BUMP=auto|major|minor|patch|X.Y.Z]
#
# Bumps __version__, adds the new version to CHANGELOG.md with git-changelog
# (settings in pyproject.toml), commits and tags vX.Y.Z.  Pushing and the
# GitHub release, which publishes to PyPI, stay manual; the commands are printed.

BUMP ?= auto
GIT_CHANGELOG := uvx git-changelog@2.10.0
VERSION_FILE := ft260_eeprom.py

.PHONY: check next-version release

check:
	uvx ruff@0.17.0 check
	uvx ruff@0.17.0 format --check

next-version:
	@$(GIT_CHANGELOG) --bump $(BUMP) --bumped-version

release: check
	@set -eu; \
	test -z "$$(git status --porcelain)" || { echo "error: uncommitted changes" >&2; exit 1; }; \
	test "$$(git branch --show-current)" = main || { echo "error: release from main" >&2; exit 1; }; \
	last=$$(git describe --tags --abbrev=0 --match 'v*'); \
	test -n "$$(git rev-list "$$last..HEAD")" || { echo "error: nothing to release since $$last" >&2; exit 1; }; \
	version=$$($(GIT_CHANGELOG) --bump $(BUMP) --bumped-version); \
	version=$${version#v}; \
	echo "$$version" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$$' || { echo "error: bad version '$$version'" >&2; exit 1; }; \
	! git rev-parse -q --verify "refs/tags/v$$version" >/dev/null || { echo "error: tag v$$version exists" >&2; exit 1; }; \
	echo "Releasing $$version"; \
	sed -i "s/^__version__ = \".*\"/__version__ = \"$$version\"/" $(VERSION_FILE); \
	grep -q "^__version__ = \"$$version\"" $(VERSION_FILE); \
	$(GIT_CHANGELOG) --bump "v$$version"; \
	grep -q "^## \[v$$version\]" CHANGELOG.md || { \
		echo "error: CHANGELOG.md has no v$$version entry" >&2; \
		git checkout -- $(VERSION_FILE) CHANGELOG.md; \
		exit 1; \
	}; \
	git commit -q -m "chore: release $$version" $(VERSION_FILE) CHANGELOG.md; \
	git tag -a "v$$version" -m "v$$version"; \
	git show --stat --format='%h %s' HEAD; \
	echo; \
	echo "Tagged v$$version. Check CHANGELOG.md, then publish:"; \
	echo "  git push origin main v$$version"; \
	echo "  gh release create v$$version --notes \"\$$($(GIT_CHANGELOG) --release-notes)\""
