# Contributing to PyModules

Thank you for your interest in contributing to PyModules!

## Development Setup

```bash
# Clone the repository
git clone https://github.com/pymodules/pymodules
cd pymodules

# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows

# Install in development mode with all dependencies
pip install -e ".[full]"

# Install pre-commit hooks
pre-commit install
```

## Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=pymodules --cov-report=html

# Run specific test file
pytest tests/test_core.py -v

# Skip slow/integration tests
pytest tests/ -m "not slow and not integration"
```

## Code Style

We use ruff for linting and formatting, and mypy for type checking:

```bash
# Format code
ruff format pymodules tests

# Lint code
ruff check pymodules tests

# Type check
mypy pymodules
```

## Pull Request Process

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make your changes with tests
4. Ensure all tests pass: `pytest tests/ -v`
5. Ensure code passes linting: `ruff check pymodules`
6. Ensure types are correct: `mypy pymodules`
7. Commit with a descriptive message
8. Push and open a pull request

## Commit Message Format

We follow conventional commits:

- `feat:` New features
- `fix:` Bug fixes
- `docs:` Documentation changes
- `test:` Test additions/modifications
- `refactor:` Code refactoring
- `chore:` Maintenance tasks

## Questions?

Open an issue or start a discussion on GitHub.
