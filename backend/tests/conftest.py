def pytest_configure(config):
    # also registered in the root pyproject.toml, which is not inside the container
    config.addinivalue_line("markers", "openmc: needs OpenMC, runs inside the backend container")
