PYTHON ?= python3

.PHONY: test compile demo clean

test:
	$(PYTHON) -m unittest discover -s tests -v

compile:
	$(PYTHON) -m compileall -q vacancy_radar tests

demo:
	$(PYTHON) -m vacancy_radar --offline fixtures/vacancies.json --top 5

clean:
	rm -rf build dist .eggs *.egg-info
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
