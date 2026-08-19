import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from jose import jwt

load_dotenv()

logger = logging.getLogger(__name__)

AGENCY_ZOOM_BASE_URL = os.getenv("AGENCY_ZOOM_BASE_URL", "https://api.agencyzoom.com")
AGENCY_ZOOM_LOGIN_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/auth/login"
AGENCY_ZOOM_TASKS_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/tasks"
AGENCY_ZOOM_CUSTOMERS_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/customers"
AGENCY_ZOOM_LEADS_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/leads"
# Agency Zoom lists leads at /leads/list. /leads/search is not an endpoint and
# answers 404, which is why leads never reached the dashboard.
AGENCY_ZOOM_LEADS_LIST_URL = f"{AGENCY_ZOOM_LEADS_URL}/list"
AGENCY_ZOOM_EMPLOYEES_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/employees"

# Stripped, because a credential pasted into the hosting dashboard easily
# carries a trailing space or newline, which Agency Zoom rejects as a bad login
USERNAME = (os.getenv("USER_NAME") or "").strip()
PASSWORD = (os.getenv("PASSWORD") or "").strip()
REQUEST_TIMEOUT = int(os.getenv("AGENCY_ZOOM_TIMEOUT", "30"))
# Follow-ups are worked in the morning, so a task with no date picked lands at
# the start of the next business day rather than whenever the call happened.
TASK_DUE_TIME = os.getenv("AGENCY_ZOOM_TASK_DUE_TIME", "09:00:00")
TASK_TITLE = os.getenv("AGENCY_ZOOM_TASK_TITLE", "Call follow-up")
BUSINESS_TZ = os.getenv("BUSINESS_TZ", "America/Los_Angeles")
TOKEN_REFRESH_BUFFER_SECONDS = int(os.getenv("AGENCY_ZOOM_TOKEN_REFRESH_BUFFER_SECONDS", "60"))
UNKNOWN_VALUE = "Unknown"

# Model placeholders that should read as empty rather than being shown to an agent
PLACEHOLDER_VALUES = {
    "not mentioned", "none", "none mentioned", "n/a", "na", "not applicable",
    "not provided", "not specified", "not discussed", "no follow-up needed",
    "no follow up needed", "no follow-up required", "no follow-up tasks",
    "no tasks", "unknown", "no data available", "-",
}

_cached_jwt_token: Optional[str] = None
_cached_jwt_expiry: Optional[datetime] = None
_cached_employee_lookup: Dict[str, Any] = {
    "employees": [],
    "by_phone": {},
    "by_name": {},
}


# Normalize optional string values used in Agency Zoom payloads.
def _clean_string(value: Any) -> Optional[str]:
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


# Normalize values expected to be numeric IDs.
def _clean_int(value: Any) -> Optional[int]:
    cleaned = _clean_string(value)
    if not cleaned:
        return None
    if cleaned.isdigit():
        return int(cleaned)
    return None


# Strip formatting from phone numbers before customer lookup.
def _normalize_phone_number(value: Any) -> Optional[str]:
    cleaned = _clean_string(value)
    if not cleaned:
        return None
    return re.sub(r"[^\d+]", "", cleaned)


# Normalize stored follow-up text into newline-separated task items.
def normalize_follow_up_task(value: Any) -> Optional[str]:
    if value is None:
        return None

    normalized = str(value).strip()
    if not normalized:
        return None

    normalized = normalized.replace("â€¢", "\n").replace("Â·", "\n")
    normalized = normalized.replace(";", "\n")
    lines = [line.strip(" \t-") for line in normalized.splitlines() if line.strip(" \t-")]
    # "NOT MENTIONED" and friends mean there is no task; show nothing instead
    lines = [line for line in lines if line.strip().strip(".").lower() not in PLACEHOLDER_VALUES]
    return "\n".join(lines) if lines else None


# Read the JWT expiry time without verifying the token signature.
def _get_jwt_expiry(jwt_token: str) -> Optional[datetime]:
    try:
        claims = jwt.get_unverified_claims(jwt_token)
        exp = claims.get("exp")
        if not exp:
            return None
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except Exception:
        return None


# Check whether the cached Agency Zoom token can still be reused.
def _is_cached_token_valid() -> bool:
    if not _cached_jwt_token or not _cached_jwt_expiry:
        return False

    refresh_time = _cached_jwt_expiry - timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS)
    return datetime.now(timezone.utc) < refresh_time


# Log in to Agency Zoom and reuse a cached token until refresh time.
def zomm_agency_login() -> str:
    global _cached_jwt_token, _cached_jwt_expiry

    if _is_cached_token_valid():
        return _cached_jwt_token

    if not USERNAME or not PASSWORD:
        raise ValueError("Agency Zoom credentials are missing. Set USER_NAME and PASSWORD in the environment.")

    response = requests.post(
        AGENCY_ZOOM_LOGIN_URL,
        json={
            "username": USERNAME,
            "password": PASSWORD,
            "version": "123232313",
        },
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )

    # A bare "400 Bad Request" says nothing about what to fix, and Agency Zoom
    # explains itself in the body, so pass that explanation on.
    if response.status_code >= 400:
        detail = (response.text or "").strip()[:300]
        if response.status_code in (400, 401, 403):
            raise ValueError(
                f"Agency Zoom rejected the login for {USERNAME or '(no username set)'} "
                f"({response.status_code}). Check the USER_NAME and PASSWORD variables. "
                f"Agency Zoom said: {detail or 'nothing'}")
        raise ValueError(f"Agency Zoom login failed ({response.status_code}): {detail or 'no detail returned'}")

    response_data = response.json()
    jwt_token = response_data.get("jwt")
    if not jwt_token:
        raise ValueError(f"Agency Zoom login succeeded but no jwt was returned: {response_data}")

    _cached_jwt_token = jwt_token
    _cached_jwt_expiry = _get_jwt_expiry(jwt_token)
    return jwt_token


def _raise_for_agency_zoom(response: requests.Response, action: str) -> None:
    """Fail with what Agency Zoom said, not just the status line.

    "500 Server Error ... /v1/api/tasks" gives nothing to act on; Agency Zoom
    puts the reason in the body, which raise_for_status throws away.
    """
    if response.status_code < 400:
        return

    detail = (response.text or "").strip()[:400]
    raise requests.HTTPError(
        f"Agency Zoom could not {action} ({response.status_code}): {detail or 'no detail returned'}",
        response=response,
    )


# Convert HTTP responses into JSON data or plain text payloads.
def _parse_response(response: requests.Response) -> Dict[str, Any]:
    if not response.content:
        return {"success": True}

    content_type = response.headers.get("Content-Type", "")
    if "application/json" in content_type:
        return response.json()

    return {"success": True, "response_text": response.text}


# Create a task in Agency Zoom for a follow-up action.
def create_agency_zoom_task(
    title: str,
    due_date: Optional[str] = None,
    task_datetime: Optional[str] = None,
    comments: Optional[str] = None,
    time_specific: Optional[bool] = None,
    customer_name: Optional[str] = None,
    customer_id: Optional[int | str] = None,
    customer_type: Optional[str] = None,
    duration: Optional[int] = None,
    assignee_id: Optional[int | str] = None,
    assignees: Optional[list[dict[str, Any]]] = None,
    jwt_token: Optional[str] = None,
) -> Dict[str, Any]:
    token = jwt_token or zomm_agency_login()
    payload: Dict[str, Any] = {"title": title}

    optional_fields = {
        "dueDate": _clean_string(due_date),
        "taskDateTime": _clean_string(task_datetime),
        "comments": _clean_string(comments),
        "timeSpecific": time_specific,
        "customerName": _clean_string(customer_name),
        # Numeric ids go as numbers, matching assigneeId; a quoted id is a
        # likely reason for the API to fail on its own side
        "customerId": _clean_int(customer_id) or _clean_string(customer_id),
        "customerType": _clean_string(customer_type),
        "duration": duration,
        "assigneeId": _clean_int(assignee_id),
        "assignees": assignees,
    }
    payload.update({key: value for key, value in optional_fields.items() if value not in (None, "", [])})

    logger.info("Creating Agency Zoom task: %s", payload)
    return _post_task_payload(payload, token)


# Optional parts of a task, dropped one group at a time when the API refuses
# the whole thing. The task itself is worth more than the extras attached to it.
_TASK_FALLBACKS = [
    ("the assignee", ("assigneeId", "assignees")),
    ("the customer link", ("customerId", "customerType")),
    ("the scheduling details", ("taskDateTime", "timeSpecific", "duration")),
]


def _post_task_payload(payload: Dict[str, Any], token: str) -> Dict[str, Any]:
    """Create the task, narrowing the payload until the API accepts it.

    Agency Zoom answers a payload it dislikes with a 500 and no field name, so
    a single bad value blocks the whole approval. Retrying without each
    optional group in turn gets the follow-up created and names, in the log,
    whichever part the API would not take.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    title = payload.get("title")
    attempt = dict(payload)
    dropped: list[str] = []
    first_error: Optional[requests.HTTPError] = None

    for label, keys in [(None, ())] + _TASK_FALLBACKS:
        if label is not None:
            if not any(key in attempt for key in keys):
                continue  # nothing of this kind was being sent anyway
            for key in keys:
                attempt.pop(key, None)
            dropped.append(label)

        response = requests.post(
            AGENCY_ZOOM_TASKS_URL, json=attempt, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code < 400:
            if dropped:
                logger.warning(
                    "Agency Zoom would not accept %s on task %r; created it without %s",
                    " or ".join(dropped), title, dropped[-1],
                )
            return _parse_response(response)

        try:
            _raise_for_agency_zoom(response, f"create the task \"{title}\"")
        except requests.HTTPError as exc:
            if first_error is None:
                first_error = exc
            # Only a server-side refusal is worth narrowing; bad credentials
            # and missing permissions fail the same way every time.
            if response.status_code < 500:
                raise

    raise first_error


# Fetch an Agency Zoom customer record by phone number.
def get_agency_zoom_customer_by_phone(
    phone: str,
    jwt_token: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_phone = _clean_string(phone)
    if not normalized_phone:
        raise ValueError("Phone number is required to fetch Agency Zoom customer details.")

    token = jwt_token or zomm_agency_login()
    response = requests.post(
        AGENCY_ZOOM_CUSTOMERS_URL,
        json={"phone": normalized_phone},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return _parse_response(response)


# Reduce any phone value to its last ten digits for reliable comparison.
def _last_ten_digits(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


# Common US phone formats a number might be stored under in Agency Zoom.
def _phone_variants(phone: Any) -> list[str]:
    digits = _last_ten_digits(phone)
    if len(digits) < 10:
        cleaned = _clean_string(phone)
        return [cleaned] if cleaned else []
    return [
        f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
        digits,
        f"+1{digits}",
        f"1{digits}",
        f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
    ]


# Pull record lists out of the various response shapes Agency Zoom returns.
def _extract_candidate_records(response_data: Any) -> list[dict[str, Any]]:
    if isinstance(response_data, list):
        return [record for record in response_data if isinstance(record, dict)]
    if isinstance(response_data, dict):
        for key in ["customers", "leads", "data", "results", "items"]:
            candidate = response_data.get(key)
            if isinstance(candidate, list):
                return [record for record in candidate if isinstance(record, dict)]
            if isinstance(candidate, dict):
                nested = _extract_candidate_records(candidate)
                if nested:
                    return nested
        if any(key in response_data for key in ("id", "customerId", "customerID", "leadId", "leadID")):
            return [response_data]
    return []


# Condense an Agency Zoom customer/lead record into a uniform summary dict.
def _candidate_summary(record: dict[str, Any], record_type: str) -> dict[str, Any]:
    first = _clean_string(record.get("firstName") or record.get("firstname"))
    last = _clean_string(record.get("lastName") or record.get("lastname"))
    name = (
        _clean_string(record.get("name"))
        or " ".join(part for part in [first, last] if part)
        or _clean_string(record.get("businessName"))
        or _clean_string(record.get("companyName"))
    )
    # Leads often keep the mobile in secondaryPhone, and a call from that number
    # has to match the lead just as well as the primary one does.
    phones: list[str] = []
    for phone_key in ("phone", "phoneNumber", "mobile", "cellPhone", "homePhone", "secondaryPhone"):
        candidate_phone = _clean_string(record.get(phone_key))
        if candidate_phone and candidate_phone not in phones:
            phones.append(candidate_phone)
    phone = phones[0] if phones else None
    record_id = None
    for key in ("id", "customerId", "customerID", "leadId", "leadID"):
        if record.get(key):
            record_id = str(record[key])
            break
    return {
        "id": record_id,
        "type": record_type,
        "name": name,
        "phone": phone,
        "phones": phones,
        "email": _clean_string(record.get("email")),
    }


# Agency Zoom's search ignores the query text and returns the first page of
# everything, so the whole directory is paged in, cached briefly, and filtered
# here. That is what lets a search for "Garcia" actually return the Garcias.
_directory_cache: Dict[str, Any] = {"customers": None, "leads": None, "fetched_at": None}
DIRECTORY_CACHE_SECONDS = int(os.getenv("AGENCY_ZOOM_CACHE_SECONDS", "600"))
DIRECTORY_PAGE_SIZE = int(os.getenv("AGENCY_ZOOM_PAGE_SIZE", "100"))
DIRECTORY_MAX_PAGES = int(os.getenv("AGENCY_ZOOM_MAX_PAGES", "60"))


def _fetch_all_pages(url: str, record_type: str, jwt_token: str) -> list[dict[str, Any]]:
    """Page through a listing endpoint until it stops returning new records.

    Agency Zoom numbers pages from 0, so a loop starting at 1 quietly drops the
    first page of the directory.
    """
    headers = {"Authorization": f"Bearer {jwt_token}", "Content-Type": "application/json"}
    found: dict[str, dict[str, Any]] = {}

    for page in range(DIRECTORY_MAX_PAGES):
        try:
            response = requests.post(
                url,
                json={"page": page, "pageSize": DIRECTORY_PAGE_SIZE},
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            )
            _raise_for_agency_zoom(response, f"list {record_type}s (page {page})")
            records = _extract_candidate_records(_parse_response(response))
        except Exception as exc:
            # A wrong URL used to look exactly like an empty directory. Say which
            # one happened, so the next broken endpoint is not silent for weeks.
            logger.warning("Agency Zoom %s listing failed on page %s: %s", record_type, page, exc)
            break

        added = 0
        for record in records:
            summary = _candidate_summary(record, record_type)
            if summary["id"] and summary["id"] not in found:
                found[summary["id"]] = summary
                added += 1
        if not added:
            break

    return list(found.values())


def _load_directory(jwt_token: Optional[str] = None) -> Dict[str, list[dict[str, Any]]]:
    now = datetime.now(timezone.utc)
    fetched_at = _directory_cache.get("fetched_at")
    if (
        _directory_cache.get("customers") is not None
        and fetched_at is not None
        and (now - fetched_at).total_seconds() < DIRECTORY_CACHE_SECONDS
    ):
        return {"customers": _directory_cache["customers"], "leads": _directory_cache["leads"]}

    token = jwt_token or zomm_agency_login()
    customers = _fetch_all_pages(AGENCY_ZOOM_CUSTOMERS_URL, "customer", token)
    leads = _fetch_all_pages(AGENCY_ZOOM_LEADS_LIST_URL, "lead", token)
    _directory_cache.update({"customers": customers, "leads": leads, "fetched_at": now})
    return {"customers": customers, "leads": leads}


def clear_agency_zoom_directory_cache() -> None:
    _directory_cache.update({"customers": None, "leads": None, "fetched_at": None})


def _record_phones(record: dict[str, Any]) -> list[str]:
    phones = record.get("phones")
    if isinstance(phones, list) and phones:
        return phones
    single = record.get("phone")
    return [single] if single else []


def _matches(record: dict[str, Any], query: str) -> bool:
    digits = _last_ten_digits(query)
    if len(digits) >= 10:
        return any(_last_ten_digits(value) == digits for value in _record_phones(record))
    needle = query.strip().lower()
    haystack = " ".join(
        str(record.get(field) or "") for field in ("name", "email", "phone")
    ).lower()
    # every word must appear, so "maria garcia" does not match every Maria
    return all(word in haystack for word in needle.split())


def search_agency_zoom_customers(query: Any, jwt_token: Optional[str] = None) -> list[dict[str, Any]]:
    cleaned = _clean_string(query)
    if not cleaned:
        return []
    token = jwt_token or zomm_agency_login()

    # An exact phone lookup is cheap and authoritative, so try it first
    if len(_last_ten_digits(cleaned)) >= 10:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        for attempt in _phone_variants(cleaned):
            try:
                response = requests.post(
                    AGENCY_ZOOM_CUSTOMERS_URL, json={"phone": attempt}, headers=headers, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                exact = [
                    summary for summary in
                    (_candidate_summary(record, "customer")
                     for record in _extract_candidate_records(_parse_response(response)))
                    if summary["id"] and _matches(summary, cleaned)
                ]
                if exact:
                    return exact
            except Exception:
                continue

    directory = _load_directory(token)
    return [record for record in directory["customers"] if _matches(record, cleaned)]


# Leads are filtered from the same cached directory, with the same authoritative
# phone shortcut the customer search uses.
def search_agency_zoom_leads(query: Any, jwt_token: Optional[str] = None) -> list[dict[str, Any]]:
    cleaned = _clean_string(query)
    if not cleaned:
        return []
    token = jwt_token or zomm_agency_login()

    # customerPhoneList matches exactly on Agency Zoom's side, so a known caller
    # is found without paging the whole lead list first.
    if len(_last_ten_digits(cleaned)) >= 10:
        try:
            response = requests.post(
                AGENCY_ZOOM_LEADS_LIST_URL,
                json={
                    "customerPhoneList": _phone_variants(cleaned),
                    "page": 0,
                    "pageSize": DIRECTORY_PAGE_SIZE,
                },
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            _raise_for_agency_zoom(response, "search leads by phone")
            exact = [
                summary for summary in
                (_candidate_summary(record, "lead")
                 for record in _extract_candidate_records(_parse_response(response)))
                if summary["id"] and _matches(summary, cleaned)
            ]
            if exact:
                return exact
        except Exception as exc:
            logger.warning("Agency Zoom lead phone search failed: %s", exc)

    directory = _load_directory(token)
    return [record for record in directory["leads"] if _matches(record, cleaned)]


# Find the best customer-or-lead match for a phone number.
# Prefers a candidate whose stored phone shares the same last ten digits;
# accepts a sole candidate otherwise; returns None when ambiguous or absent.
def find_agency_zoom_match(phone: Any, jwt_token: Optional[str] = None) -> Optional[dict[str, Any]]:
    digits = _last_ten_digits(phone)
    if len(digits) < 10:
        return None

    token = jwt_token or zomm_agency_login()
    candidates = search_agency_zoom_customers(phone, jwt_token=token)
    candidates += search_agency_zoom_leads(phone, jwt_token=token)
    if not candidates:
        return None

    # Only an exact phone match may be linked automatically. Agency Zoom search
    # can return unrelated records when it does not recognise a filter, and a
    # sole result is not evidence - a wrong link puts call notes on the wrong
    # client file. Anything less certain is left for a human to link by hand.
    for candidate in candidates:
        if any(_last_ten_digits(value) == digits for value in _record_phones(candidate)):
            return candidate
    return None


# Create a CRM note on an existing Agency Zoom customer or lead.
# A lead id posted to the customer endpoint is rejected with "Customer does not
# belong to the current user", so the record type decides the URL.
def create_agency_zoom_customer_note(
    customer_id: int | str,
    note: str,
    jwt_token: Optional[str] = None,
    author_id: Optional[str] = None,
    record_type: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_customer_id = _clean_string(customer_id)
    normalized_note = _clean_string(note)

    is_lead = (_clean_string(record_type) or "customer").lower() == "lead"
    label = "lead" if is_lead else "customer"

    if not normalized_customer_id:
        raise ValueError(f"{label.capitalize()} id is required to create an Agency Zoom note.")
    if not normalized_note:
        raise ValueError(f"Note is required to create an Agency Zoom {label} note.")

    token = jwt_token or zomm_agency_login()
    payload: Dict[str, Any] = {"note": normalized_note}
    if _clean_int(author_id):
        # Harmless if Agency Zoom ignores these; useful if it honours either
        payload["userId"] = _clean_int(author_id)
        payload["employeeId"] = _clean_int(author_id)

    base_url = AGENCY_ZOOM_LEADS_URL if is_lead else AGENCY_ZOOM_CUSTOMERS_URL
    response = requests.post(
        f"{base_url}/{normalized_customer_id}/notes",
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    _raise_for_agency_zoom(response, f"add a note to {label} {normalized_customer_id}")
    return _parse_response(response)


# Extract a task ID from possible Agency Zoom response shapes.
def _extract_agency_zoom_task_id(response_data: Any) -> Optional[str]:
    if not isinstance(response_data, dict):
        return None

    for key in ["id", "taskId", "taskID"]:
        value = response_data.get(key)
        if value:
            return str(value)

    for nested in [response_data.get("data"), response_data.get("task")]:
        if isinstance(nested, dict):
            for key in ["id", "taskId", "taskID"]:
                value = nested.get(key)
                if value:
                    return str(value)

    return None


# Extract a customer ID from possible Agency Zoom response shapes.
def _extract_agency_zoom_customer_id(response_data: Any) -> Optional[str]:
    if not isinstance(response_data, dict):
        return None

    for key in ["id", "customerId", "customerID"]:
        value = response_data.get(key)
        if value:
            return str(value)

    for key in ["data", "customer", "result"]:
        nested = response_data.get(key)
        if isinstance(nested, dict):
            for customer_key in ["id", "customerId", "customerID"]:
                value = nested.get(customer_key)
                if value:
                    return str(value)

    for candidate in [response_data.get("customers"), response_data.get("data"), response_data.get("results")]:
        if isinstance(candidate, list) and candidate:
            first_item = candidate[0]
            if isinstance(first_item, dict):
                for customer_key in ["id", "customerId", "customerID"]:
                    value = first_item.get(customer_key)
                    if value:
                        return str(value)

    return None


# Extract employee records from supported Agency Zoom response shapes.
def _extract_employee_records(response_data: Any) -> list[dict[str, Any]]:
    if isinstance(response_data, list):
        return [employee for employee in response_data if isinstance(employee, dict)]

    if isinstance(response_data, dict):
        for key in ["employees", "data", "results"]:
            candidate = response_data.get(key)
            if isinstance(candidate, list):
                return [employee for employee in candidate if isinstance(employee, dict)]

    return []


# Normalize employee names for case-insensitive lookup keys.
def _normalize_employee_name(value: Any) -> Optional[str]:
    cleaned = _clean_string(value)
    if not cleaned:
        return None
    return " ".join(cleaned.lower().split())


# Build phone and name lookup maps from the employee list.
def _build_employee_lookup(employee_list: Any) -> Dict[str, Any]:
    employees = _extract_employee_records(employee_list)
    by_phone: Dict[str, Dict[str, Any]] = {}
    by_name: Dict[str, Dict[str, Any]] = {}

    for employee in employees:
        employee_phone = format_phone_number_for_comparison(employee.get("phone", ""))
        if employee_phone:
            by_phone[employee_phone] = employee

        firstname = _clean_string(employee.get("firstname"))
        lastname = _clean_string(employee.get("lastname"))
        full_name = " ".join(part for part in [firstname, lastname] if part)

        for candidate in [full_name, firstname, lastname, employee.get("email")]:
            normalized_candidate = _normalize_employee_name(candidate)
            if normalized_candidate:
                by_name[normalized_candidate] = employee

    return {
        "employees": employees,
        "by_phone": by_phone,
        "by_name": by_name,
    }


# Store the latest employee lookup maps in the in-memory cache.
def _set_cached_employee_lookup(employee_list: Any) -> Dict[str, Any]:
    global _cached_employee_lookup
    _cached_employee_lookup = _build_employee_lookup(employee_list)

    print("Employee lookup cache updated.", _cached_employee_lookup)
    return _cached_employee_lookup


# Clear the in-memory employee lookup cache.
def clear_agency_zoom_employee_cache() -> None:
    global _cached_employee_lookup
    _cached_employee_lookup = {
        "employees": [],
        "by_phone": {},
        "by_name": {},
    }


# Return the cached employee lookup maps when available.
def get_cached_agency_zoom_employee_lookup() -> Dict[str, Any]:
    if _cached_employee_lookup.get("employees"):
        return _cached_employee_lookup
    return {}



# List Agency Zoom employees so each app user can be mapped to one.
def list_agency_zoom_employees_with_error(
    jwt_token: Optional[str] = None,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Employees plus the reason the list is empty, so the UI can explain itself."""
    try:
        token = jwt_token or zomm_agency_login()
    except Exception as exc:
        return [], f"Could not sign in to Agency Zoom: {exc}"

    try:
        response = requests.get(
            AGENCY_ZOOM_EMPLOYEES_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = _parse_response(response)
        employees = _extract_employee_records(payload)
        if not employees:
            return [], f"Agency Zoom returned no employees ({str(payload)[:160]})"
    except Exception as exc:
        return [], f"Agency Zoom employee lookup failed: {exc}"

    listed = []
    for employee in employees:
        first = _clean_string(employee.get("firstname")) or _clean_string(employee.get("firstName"))
        last = _clean_string(employee.get("lastname")) or _clean_string(employee.get("lastName"))
        name = " ".join(part for part in [first, last] if part) or _clean_string(employee.get("name"))
        identifier = _clean_string(employee.get("id"))
        if identifier:
            listed.append({
                "id": identifier,
                "name": name or f"Employee {identifier}",
                "email": _clean_string(employee.get("email")),
            })
    return sorted(listed, key=lambda item: item["name"].lower()), None


def list_agency_zoom_employees(jwt_token: Optional[str] = None) -> list[dict[str, Any]]:
    employees, _ = list_agency_zoom_employees_with_error(jwt_token=jwt_token)
    return employees


# Find the Agency Zoom employee whose email matches an app user's email.
def find_agency_zoom_employee_by_email(email: Any, jwt_token: Optional[str] = None) -> Optional[dict[str, Any]]:
    needle = (_clean_string(email) or "").lower()
    if not needle:
        return None
    for employee in list_agency_zoom_employees(jwt_token=jwt_token):
        if (employee.get("email") or "").lower() == needle:
            return employee
    return None


def _business_tz():
    """The agency's own timezone, so "tomorrow" is not decided in UTC."""
    try:
        return ZoneInfo(BUSINESS_TZ)
    except Exception:
        return timezone.utc


def _task_due_dates(chosen: Any) -> tuple[str, str]:
    """Return the due date and the due date-and-time for a task.

    Agency Zoom took a date-and-time in its dueDate field and fell back to
    today, losing the date the agent picked, so the two fields are now sent in
    their own formats: dueDate is the day, taskDateTime carries the time.
    """
    chosen_date = (_clean_string(chosen) or "").split(" ")[0]
    time_of_day = TASK_DUE_TIME

    if chosen_date:
        try:
            datetime.strptime(chosen_date, "%Y-%m-%d")
        except ValueError:
            logger.warning("Ignoring unusable Agency Zoom due date %r", chosen)
            chosen_date = ""
        else:
            # keep a time the agent typed alongside the date
            rest = (_clean_string(chosen) or "")[len(chosen_date):].strip()
            if rest:
                time_of_day = rest

    if not chosen_date:
        chosen_date = (datetime.now(_business_tz()) + timedelta(days=1)).strftime("%Y-%m-%d")

    return chosen_date, f"{chosen_date} {time_of_day}"


# Create one Agency Zoom task for each follow-up line on a transcript.
def create_agency_zoom_task_for_transcript(
    transcript: Any,
    task_text: str,
    due_date: Optional[str] = None,
    assignee_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> Optional[str]:
    """Create one follow-up task, with its own due date, and return its id.

    Tasks are added one at a time and only when somebody presses Add, so each
    carries the date that task is actually due rather than one date standing in
    for every follow-up on the call.
    """
    text = _clean_string(task_text)
    if not text:
        raise ValueError("A follow-up task needs some text before it can be added.")

    resolved_due, resolved_datetime = _task_due_dates(
        _clean_string(due_date) or getattr(transcript, "agency_zoom_due_date", None))

    context_lines = [
        f"Client Name: {_clean_string(getattr(transcript, 'client_name', None)) or UNKNOWN_VALUE}",
        f"Client Number: {_clean_string(getattr(transcript, 'client_number', None)) or UNKNOWN_VALUE}",
        f"Policy Type: {_clean_string(getattr(transcript, 'policy_type', None)) or UNKNOWN_VALUE}",
        f"Reason For Call: {_clean_string(getattr(transcript, 'reason_for_call', None)) or UNKNOWN_VALUE}",
        f"Handled by: {_clean_string(agent_name) or UNKNOWN_VALUE}",
        f"Recording ID: {_clean_string(getattr(transcript, 'recordingID', None)) or UNKNOWN_VALUE}",
        f"Transcript ID: {getattr(transcript, 'id', UNKNOWN_VALUE)}",
        "The full call write-up is on the customer's notes.",
    ]

    jwt_token = zomm_agency_login()
    resolved_assignee = _clean_int(assignee_id) or resolve_agency_zoom_assignee_id(
        transcript, jwt_token=jwt_token)

    customer_name = _clean_string(getattr(transcript, "client_name", None))
    customer_id = customer_type = None
    match = resolve_transcript_agency_zoom_match(transcript, jwt_token=jwt_token)
    if match:
        customer_id = match.get("id")
        customer_type = match.get("type") or "customer"
        customer_name = match.get("name") or customer_name

    response_data = create_agency_zoom_task(
        title=f"{TASK_TITLE}: {customer_name}" if customer_name else TASK_TITLE,
        due_date=resolved_due,
        task_datetime=resolved_datetime,
        comments="\n".join([text, ""] + context_lines),
        time_specific=True,
        customer_name=customer_name,
        customer_id=customer_id,
        customer_type=customer_type,
        assignee_id=resolved_assignee,
        jwt_token=jwt_token,
    )
    return _extract_agency_zoom_task_id(response_data)


def create_agency_zoom_tasks_for_transcript(
    transcript: Any,
    assignee_id: Optional[str] = None,
    agent_name: Optional[str] = None,
) -> list[str]:
    normalized_tasks = normalize_follow_up_task(getattr(transcript, "follow_up_task", None))
    if not normalized_tasks:
        return []

    due_date, due_datetime = _task_due_dates(getattr(transcript, "agency_zoom_due_date", None))

    # Enough context to act on the task, and no more. The call write-up belongs
    # in the customer note; repeating it here made every task read as a copy of
    # the note rather than a thing to do.
    context_lines = [
        f"Client Name: {_clean_string(getattr(transcript, 'client_name', None)) or UNKNOWN_VALUE}",
        f"Client Number: {_clean_string(getattr(transcript, 'client_number', None)) or UNKNOWN_VALUE}",
        f"Policy Type: {_clean_string(getattr(transcript, 'policy_type', None)) or UNKNOWN_VALUE}",
        f"Reason For Call: {_clean_string(getattr(transcript, 'reason_for_call', None)) or UNKNOWN_VALUE}",
        f"Handled by: {_clean_string(agent_name) or UNKNOWN_VALUE}",
        f"Recording ID: {_clean_string(getattr(transcript, 'recordingID', None)) or UNKNOWN_VALUE}",
        f"Transcript ID: {getattr(transcript, 'id', UNKNOWN_VALUE)}",
        "The full call write-up is on the customer's notes.",
    ]

    jwt_token = zomm_agency_login()
    created_task_ids: list[str] = []
    customer_name = _clean_string(getattr(transcript, "client_name", None))
    customer_id: Optional[str] = None
    customer_type: Optional[str] = None
    # The agent the transcript is assigned to owns the task; fall back to the
    # older name and phone matching only when no mapping exists.
    resolved_assignee = _clean_int(assignee_id) or resolve_agency_zoom_assignee_id(
        transcript, jwt_token=jwt_token)


    match = resolve_transcript_agency_zoom_match(transcript, jwt_token=jwt_token)
    if match:
        customer_id = match.get("id")
        customer_type = match.get("type") or "customer"
        customer_name = match.get("name") or customer_name

    # A short title with the detail underneath, rather than a whole paragraph
    # crammed into the title of the task list.
    title = f"{TASK_TITLE}: {customer_name}" if customer_name else TASK_TITLE

    for task_line in normalized_tasks.splitlines():
        task_text = task_line.strip()
        if not task_text:
            continue

        response_data = create_agency_zoom_task(
            title=title,
            due_date=due_date,
            task_datetime=due_datetime,
            comments="\n".join([task_text, ""] + context_lines),
            time_specific=True,
            customer_name=customer_name,
            customer_id=customer_id,
            customer_type=customer_type,
            assignee_id=resolved_assignee,
            jwt_token=jwt_token,
        )
        task_id = _extract_agency_zoom_task_id(response_data)
        if task_id:
            created_task_ids.append(task_id)

    return created_task_ids


# Resolve the Agency Zoom record for a transcript: a manually linked match
# wins, otherwise fall back to searching every phone number we know for the call.
def resolve_transcript_agency_zoom_match(transcript: Any, jwt_token: Optional[str] = None) -> Optional[dict[str, Any]]:
    linked_id = _clean_string(getattr(transcript, "agency_zoom_customer_id", None))
    if linked_id:
        return {
            "id": linked_id,
            "type": _clean_string(getattr(transcript, "agency_zoom_customer_type", None)) or "customer",
            "name": _clean_string(getattr(transcript, "agency_zoom_customer_name", None))
            or _clean_string(getattr(transcript, "client_name", None)),
            "phone": _clean_string(getattr(transcript, "client_number", None)),
            "source": "linked",
        }

    token = jwt_token or zomm_agency_login()
    for attribute in ("client_number", "caller_number", "to_phoneNumber"):
        match = find_agency_zoom_match(getattr(transcript, attribute, None), jwt_token=token)
        if match:
            match["source"] = f"auto:{attribute}"
            return match
    return None


# Create an Agency Zoom customer note using the transcript CRM note.
def create_agency_zoom_customer_note_for_transcript(
    transcript: Any,
    agent_name: Optional[str] = None,
    employee_id: Optional[str] = None,
) -> Dict[str, Any]:
    note = _clean_string(getattr(transcript, "crm_note", None))
    if not note:
        raise ValueError("crm_note is required to create an Agency Zoom customer note.")

    # The analysis's stock disclaimers never reach the CRM, whatever is stored
    # here -- a customer file is for what happened on the call.
    from client_note_email import strip_boilerplate
    note = strip_boilerplate(note) or note

    jwt_token = zomm_agency_login()
    match = resolve_transcript_agency_zoom_match(transcript, jwt_token=jwt_token)
    if not match or not match.get("id"):
        raise ValueError(
            "No Agency Zoom customer or lead matched this call. Use \"Find in Agency Zoom\" on the "
            "transcript page to link the right record, then approve again."
        )

    # Name the client at the top, from the record this call is linked to. The
    # analysis sometimes writes a note that reads as though the agent were the
    # caller, and a note on a customer file that names the wrong person as the
    # client is worse than one that names nobody.
    client = _clean_string(match.get("name"))
    if client and not note.lower().startswith("client:"):
        note = f"Client: {client}\n\n{note}"

    # The API posts notes as the integration user, so the agent is named in the
    # note itself and passed as an author id where the API accepts one. Between
    # this line and the one above, who called and who took the call are both
    # stated rather than left to the wording.
    author = _clean_string(agent_name)
    if author:
        note = f"{note}\n\nCall handled by {author}."

    return create_agency_zoom_customer_note(
        customer_id=match["id"],
        note=note,
        jwt_token=jwt_token,
        author_id=employee_id,
        record_type=match.get("type"),
    )

# Fetch and cache the Agency Zoom employee list.
def get_agency_zoom_employee_list(jwt_token: Optional[str] = None) -> Dict[str, Any]:
    cached_lookup = get_cached_agency_zoom_employee_lookup()
    if cached_lookup:
        return cached_lookup["employees"]

    token = jwt_token or zomm_agency_login()
    response = requests.get(
        AGENCY_ZOOM_EMPLOYEES_URL,
        headers={
            "Authorization": f"Bearer {token}",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    employee_list = _parse_response(response)
    _set_cached_employee_lookup(employee_list)
    return employee_list

# Format a phone number to match the employee list format.
def format_phone_number_for_comparison(phone: str) -> str:
    normalized_phone = _normalize_phone_number(phone)
    if not normalized_phone:
        return ""

    if len(normalized_phone) == 10:
        return f"({normalized_phone[:3]}) {normalized_phone[3:6]}-{normalized_phone[6:]}"
    elif len(normalized_phone) == 12 and normalized_phone.startswith("+1"):
        digits = normalized_phone[2:]
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    else:
        return normalized_phone

# Search the employee list by phone number.
def search_emmployee_by_phone(phone: str, employee_list: Any) -> Optional[Dict[str, Any]]:
    normalized_phone = format_phone_number_for_comparison(phone)
    if not normalized_phone:
        return None

    employee_lookup = _build_employee_lookup(employee_list)
    return employee_lookup["by_phone"].get(normalized_phone)


# Search the employee list by normalized name or email.
def search_emmployee_by_name(name: str, employee_list: Any) -> Optional[Dict[str, Any]]:
    normalized_name = _normalize_employee_name(name)
    if not normalized_name:
        return None

    employee_lookup = _build_employee_lookup(employee_list)
    return employee_lookup["by_name"].get(normalized_name)


# Resolve an Agency Zoom assignee ID from transcript owner details.
def resolve_agency_zoom_assignee_id(transcript: Any, jwt_token: Optional[str] = None) -> Optional[int]:

    print("Resolving assignee ID for transcript:", transcript)

    direct_assignee_id = _clean_int(getattr(transcript, "owner_id", None))

    print("Direct assignee ID from transcript owner_id:", direct_assignee_id)
    if direct_assignee_id:
        return direct_assignee_id

    employee_lookup = get_cached_agency_zoom_employee_lookup()

    print("Employee lookup cache:", employee_lookup)
    if not employee_lookup:
        try:
            employee_list = get_agency_zoom_employee_list(jwt_token=jwt_token)
            employee_lookup = _build_employee_lookup(employee_list)
        except Exception:
            employee_lookup = {}

    employee = None
    owner_value = _clean_string(getattr(transcript, "owner_id", None))
    print("Owner value:", owner_value)
    if owner_value:
        normalized_phone = format_phone_number_for_comparison(owner_value)
        if normalized_phone:
            employee = employee_lookup.get("by_phone", {}).get(normalized_phone)

        if not employee:
            normalized_owner_name = _normalize_employee_name(owner_value)
            if normalized_owner_name:
                employee = employee_lookup.get("by_name", {}).get(normalized_owner_name)

    if not employee:
        for candidate_name in [getattr(transcript, "to_name", None), getattr(transcript, "from_name", None)]:
            normalized_candidate = _normalize_employee_name(candidate_name)
            if normalized_candidate:
                employee = employee_lookup.get("by_name", {}).get(normalized_candidate)
                if employee:
                    break

    print("Resolved employee record:", employee)
 

    if not employee:
        return None

    return _clean_int(employee.get("id"))


#  call hte agency zoom employe list
# emp_list=get_agency_zoom_employee_list(jwt_token="eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJodHRwczovL2FwaS5hZ2VuY3l6b29tLmNvbSIsImF1ZCI6Imh0dHBzOi8vYXBpLmFnZW5jeXpvb20uY29tIiwiaWF0IjoxNzc2MjMyOTg1LCJuYmYiOjE3NzYyMzI5ODUsImV4cCI6MTc3NjMxOTM4NSwianRpIjp7ImVtYWlsIjoic29sdXRpb25zLnByb3ZpZGVyLmRldkBnbWFpbC5jb20iLCJmaXJzdG5hbWUiOiJTaGVraGFyIiwibGFzdG5hbWUiOiJTaW5naCIsInJvbGVzIjpbIlJPTEVfQUdFTkNZX09XTkVSIiwiUk9MRV9DU1IiXSwiaXNQZXJzb25hdGUiOmZhbHNlLCJwZXJzb25hdGluZ1VzZXIiOm51bGwsImFnZW5jeSI6Ik1UUTFNalU9IiwidSI6Ik1UZzBNVEl4IiwiaXNBbGxzdGF0ZSI6ZmFsc2UsImFnZW50IjoiTVRjeE56QXcifX0.6N5w8okz6qsv249G2dYRlxClO3HuuMbIqaSA4wdpng8")
# print("Fetched employee list:", emp_list)


# resolve_agency_zoom_assignee_id
# assig_id= resolve_agency_zoom_assignee_id(transcript=type("Transcript", (object,), {"owner_id": "John Doe", "to_name": "Jane Smith", "from_name": "Bob Johnson"})(), jwt_token="eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJodHRwczovL2FwaS5hZ2VuY3l6b29tLmNvbSIsImF1ZCI6Imh0dHBzOi8vYXBpLmFnZW5jeXpvb20uY29tIiwiaWF0IjoxNzc2MjMyOTg1LCJuYmYiOjE3NzYyMzI5ODUsImV4cCI6MTc3NjMxOTM4NSwianRpIjp7ImVtYWlsIjoic29sdXRpb25zLnByb3ZpZGVyLmRldkBnbWFpbC5jb20iLCJmaXJzdG5hbWUiOiJTaGVraGFyIiwibGFzdG5hbWUiOiJTaW5naCIsInJvbGVzIjpbIlJPTEVfQUdFTkNZX09XTkVSIiwiUk9MRV9DU1IiXSwiaXNQZXJzb25hdGUiOmZhbHNlLCJwZXJzb25hdGluZ1VzZXIiOm51bGwsImFnZW5jeSI6Ik1UUTFNalU9IiwidSI6Ik1UZzBNVEl4IiwiaXNBbGxzdGF0ZSI6ZmFsc2UsImFnZW50IjoiTVRjeE56QXcifX0.6N5w8okz6qsv249G2dYRlxClO3HuuMbIqaSA4wdpng8")



