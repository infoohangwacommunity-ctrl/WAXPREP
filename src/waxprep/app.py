"""HTTP application entry point for Wax Prep."""

from fastapi import FastAPI

from waxprep import __version__

app = FastAPI(title="Wax Prep", version=__version__)


@app.get("/health", tags=["system"])
def health() -> dict[str, str]:
    """Report whether the application process is running."""
    return {
        "status": "ok",
        "service": "waxprep",
        "version": __version__,
    }
