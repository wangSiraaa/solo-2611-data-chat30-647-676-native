PY ?= .venv/bin/python

.PHONY: setup data run verify verify-full clean

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

data:
	$(PY) -m tidewin.gendata

run:
	$(PY) -m tidewin.run

verify:
	$(PY) -m tidewin.verify

verify-full:
	$(PY) -m tidewin.verify --falsify --falsify-step=0.02

clean:
	rm -rf proto
