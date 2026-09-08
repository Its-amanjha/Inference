FROM inference:local
USER 0
RUN python -m pip install --no-cache-dir --no-deps pytest==9.1.1 pytest-asyncio==1.4.0 iniconfig==2.3.0 packaging==26.2 pluggy==1.6.0 pygments==2.21.0 && python -m pip check
COPY tests /app/tests
COPY pyproject.toml /app/pyproject.toml
USER 10001:10001
CMD ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"]
