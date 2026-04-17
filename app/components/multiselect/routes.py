"""
Reusable multiselect routes for deck selection.

Usage:
- GET  /components/multiselect/options?name=deck_ids&query=search&selected=id1,id2
  Returns filtered options as HTML partial

- POST /components/multiselect/control?name=deck_ids&action=select|remove&key=deck_id&selected=id1,id2
  Returns updated control (chips + dropdown) as HTML partial
"""

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from app.api.routers.pages import templates

router = APIRouter(prefix="/components/multiselect", tags=["components"])


def _get_options_from_request(request: Request, name: str) -> list[dict]:
    """Get options for a control from app state."""
    multiselect_options = getattr(request.app.state, "multiselect_options", {})
    return multiselect_options.get(name, [])


def _parse_selected(selected: str) -> list[str]:
    """Parse comma-separated selected keys."""
    if not selected or selected == "undefined":
        return []
    return [k.strip() for k in selected.split(",") if k.strip() and k.strip() != "undefined"]


def _parse_query(query: str) -> str:
    """Parse and sanitize search query."""
    if not query or query == "undefined":
        return ""
    return query.strip()


@router.get("/options", response_class=HTMLResponse)
def multiselect_options(
    request: Request,
    name: str = Query(..., description="Form field name (e.g., 'deck_ids')"),
    query: str = Query("", description="Search query"),
):
    """
    Return filtered options for the multiselect dropdown.
    Called via htmx when user types in the search input.
    """
    # Get selected from the hidden input via htmx-include
    # Hidden input has name=control_name, so we read from that param
    selected = request.query_params.get(name, "")
    selected_keys = _parse_selected(selected)

    # Sanitize query
    query = _parse_query(query)

    # Get available options from app state
    all_options = _get_options_from_request(request, name)

    # Filter by query (case-insensitive match on title)
    if query:
        query_lower = query.lower()
        options = [
            opt for opt in all_options
            if query_lower in opt.get("title", "").lower() and opt.get("key") not in selected_keys
        ]
    else:
        # Show unselected options when no query
        options = [opt for opt in all_options if opt.get("key") not in selected_keys]

    # Limit results
    options = options[:10]

    return templates.TemplateResponse(
        "multiselect-options.html",
        {
            "request": request,
            "options": options,
            "control_name": name,
            "selected": selected_keys,
        }
    )


@router.post("/control", response_class=HTMLResponse)
def multiselect_control(
    request: Request,
    name: str = Form(..., description="Form field name"),
    action: str = Form(..., description="Action: 'select' or 'remove'"),
    key: str = Form(..., description="Option key to select/remove"),
    selected: str = Form("", description="Comma-separated selected keys"),
    placeholder: str = Form("Search..."),
):
    """
    Handle select/remove actions and return updated control.
    Called via htmx when user clicks an option or removes a chip.
    """
    selected_keys = _parse_selected(selected)

    if action == "select" and key and key not in selected_keys:
        selected_keys.append(key)
    elif action == "remove" and key in selected_keys:
        selected_keys.remove(key)

    # Get options from app state
    all_options = _get_options_from_request(request, name)
    selected_objects = [opt for opt in all_options if opt.get("key") in selected_keys]

    return templates.TemplateResponse(
        "multiselect.html",
        {
            "request": request,
            "control_name": name,
            "selected_options": selected_objects,
            "selected_keys": selected_keys,
            "placeholder": placeholder,
            "is_partial": True,
        }
    )
