PYTHON ?= python

.PHONY: demo test eval serve install audit clean

demo:
	$(PYTHON) -m app.demo

test:
	$(PYTHON) -m unittest discover -s tests -v

eval:
	$(PYTHON) -m app.cli eval

serve:
	$(PYTHON) -m uvicorn app.api.app:create_app --factory --host 127.0.0.1 --port 8000 --reload

install:
	$(PYTHON) -m pip install -r requirements.txt

audit:
	$(PYTHON) -m app.cli audit --limit 20

clean:
	$(PYTHON) -c "import pathlib; [p.unlink() for p in pathlib.Path('data').glob('*.db*')]"
