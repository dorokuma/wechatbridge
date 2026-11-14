.PHONY: check

check:
	@if python3 -c "import httpx, qrcode, PIL, cryptography, yaml" >/dev/null 2>&1; then \
		python3 -m unittest discover -s tests; \
	elif command -v uv >/dev/null 2>&1; then \
		venv="$$(mktemp -d /tmp/wcb-check-venv.XXXXXX)" || exit $$?; \
		trap 'rm -rf "$$venv"' EXIT; \
		trap 'exit 130' INT; \
		trap 'exit 143' TERM; \
		uv venv -q "$$venv" && (uv pip install -q --offline --python "$$venv/bin/python" -e . || uv pip install -q --python "$$venv/bin/python" -e .) && "$$venv/bin/python" -m unittest discover -s tests; \
	else \
		python3 -m unittest discover -s tests; \
	fi
