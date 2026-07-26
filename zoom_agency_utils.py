import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv
from jose import jwt

load_dotenv()

AGENCY_ZOOM_BASE_URL = os.getenv("AGENCY_ZOOM_BASE_URL", "https://api.agencyzoom.com")
AGENCY_ZOOM_LOGIN_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/auth/login"
AGENCY_ZOOM_TASKS_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/tasks"
AGENCY_ZOOM_CUSTOMERS_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/customers"
AGENCY_ZOOM_LEADS_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/leads"
AGENCY_ZOOM_EMPLOYEES_URL = f"{AGENCY_ZOOM_BASE_URL}/v1/api/employees"

USERNAME = os.getenv("USER_NAME")
PASSWORD = os.getenv("PASSWORD")
REQUEST_TIMEOUT = int(os.getenv("AGENCY_ZOOM_TIMEOUT", "30"))
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
    response.raise_for_status()

    response_data = response.json()
    jwt_token = response_data.get("jwt")
    if not jwt_token:
        raise ValueError(f"Agency Zoom login succeeded but no jwt was returned: {response_data}")

    _cached_jwt_token = jwt_token
    _cached_jwt_expiry = _get_jwt_expiry(jwt_token)
    return jwt_token


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
        "customerId": _clean_string(customer_id),
        "customerType": _clean_string(customer_type),
        "duration": duration,
        "assigneeId": _clean_int(assignee_id),
        "assignees": assignees,
    }
    payload.update({key: value for key, value in optional_fields.items() if value not in (None, "", [])})

    print(f"Creating Agency Zoom task with payload: {payload}") 

    response = requests.post(
        AGENCY_ZOOM_TASKS_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return _parse_response(response)


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
    phone = _clean_string(
        record.get("phone")
        or record.get("phoneNumber")
        or record.get("mobile")
        or record.get("cellPhone")
        or record.get("homePhone")
    )
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
        "email": _clean_string(record.get("email")),
    }


# Search Agency Zoom customers. A phone query is retried in every common
# format; any other query is tried against each field name the API may expect,
# so a client can be found by name, business name, or email as well.
def search_agency_zoom_customers(query: Any, jwt_token: Optional[str] = None) -> list[dict[str, Any]]:
    token = jwt_token or zomm_agency_login()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    cleaned = _clean_string(query)
    if not cleaned:
        return []

    is_phone = len(_last_ten_digits(cleaned)) >= 10
    attempts = _phone_variants(cleaned) if is_phone else [cleaned]

    found: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        if not attempt:
            continue
        if is_phone:
            payloads = [{"phone": attempt}, {"searchText": attempt}, {"search": attempt}]
        else:
            payloads = [
                {"searchText": attempt},
                {"search": attempt},
                {"name": attempt},
                {"keyword": attempt},
                {"email": attempt} if "@" in attempt else {"businessName": attempt},
            ]
        for payload in payloads:
            try:
                response = requests.post(
                    AGENCY_ZOOM_CUSTOMERS_URL,
                    json={**payload, "pageSize": 50},
                    headers=headers,
                    timeout=REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                for record in _extract_candidate_records(_parse_response(response)):
                    summary = _candidate_summary(record, "customer")
                    if summary["id"] and summary["id"] not in found:
                        found[summary["id"]] = summary
            except Exception:
                continue
            if found:
                break
        if found:
            break
    return list(found.values())


# Search Agency Zoom leads, tolerating either search endpoint shape and any of
# the field names the API may accept.
def search_agency_zoom_leads(query: Any, jwt_token: Optional[str] = None) -> list[dict[str, Any]]:
    token = jwt_token or zomm_agency_login()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    cleaned = _clean_string(query)
    if not cleaned:
        return []

    is_phone = len(_last_ten_digits(cleaned)) >= 10
    attempts = _phone_variants(cleaned) if is_phone else [cleaned]

    found: dict[str, dict[str, Any]] = {}
    for attempt in attempts:
        if not attempt:
            continue
        field_sets = (
            [{"phone": attempt}, {"searchText": attempt}]
            if is_phone
            else [{"searchText": attempt}, {"search": attempt}, {"name": attempt}]
        )
        for fields in field_sets:
            for method, url in (("post", f"{AGENCY_ZOOM_LEADS_URL}/search"), ("get", AGENCY_ZOOM_LEADS_URL)):
                try:
                    if method == "post":
                        response = requests.post(
                            url, json={**fields, "pageSize": 50}, headers=headers, timeout=REQUEST_TIMEOUT)
                    else:
                        response = requests.get(
                            url, params=fields, headers=headers, timeout=REQUEST_TIMEOUT)
                    if response.status_code in (404, 405):
                        continue
                    response.raise_for_status()
                    for record in _extract_candidate_records(_parse_response(response)):
                        summary = _candidate_summary(record, "lead")
                        if summary["id"] and summary["id"] not in found:
                            found[summary["id"]] = summary
                except Exception:
                    continue
                if found:
                    break
            if found:
                break
        if found:
            break
    return list(found.values())


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
        if candidate["phone"] and _last_ten_digits(candidate["phone"]) == digits:
            return candidate
    return None


# Create a CRM note for an existing Agency Zoom customer.
def create_agency_zoom_customer_note(
    customer_id: int | str,
    note: str,
    jwt_token: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_customer_id = _clean_string(customer_id)
    normalized_note = _clean_string(note)

    if not normalized_customer_id:
        raise ValueError("Customer id is required to create an Agency Zoom customer note.")
    if not normalized_note:
        raise ValueError("Note is required to create an Agency Zoom customer note.")

    token = jwt_token or zomm_agency_login()
    response = requests.post(
        f"{AGENCY_ZOOM_CUSTOMERS_URL}/{normalized_customer_id}/notes",
        json={"note": normalized_note},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
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


# Create one Agency Zoom task for each follow-up line on a transcript.
def create_agency_zoom_tasks_for_transcript(transcript: Any) -> list[str]:
    normalized_tasks = normalize_follow_up_task(getattr(transcript, "follow_up_task", None))
    if not normalized_tasks:
        return []

    due_datetime = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    comments = "\n".join(
        [
            f"Client Name: {_clean_string(getattr(transcript, 'client_name', None)) or UNKNOWN_VALUE}",
            f"Client Number: {_clean_string(getattr(transcript, 'client_number', None)) or UNKNOWN_VALUE}",
            f"Policy Type: {_clean_string(getattr(transcript, 'policy_type', None)) or UNKNOWN_VALUE}",
            f"Reason For Call: {_clean_string(getattr(transcript, 'reason_for_call', None)) or UNKNOWN_VALUE}",
            f"CRM Note: {_clean_string(getattr(transcript, 'crm_note', None)) or UNKNOWN_VALUE}",
            f"Recording ID: {_clean_string(getattr(transcript, 'recordingID', None)) or UNKNOWN_VALUE}",
            f"Transcript ID: {getattr(transcript, 'id', UNKNOWN_VALUE)}",
        ]
    )

    jwt_token = zomm_agency_login()
    created_task_ids: list[str] = []
    customer_name = _clean_string(getattr(transcript, "client_name", None))
    customer_id: Optional[str] = None
    customer_type: Optional[str] = None
    assignee_id = resolve_agency_zoom_assignee_id(transcript, jwt_token=jwt_token)
    print(f"\n\n\nResolved assignee ID: {assignee_id} for transcript with client number: {getattr(transcript, 'client_number', None)}")


    match = resolve_transcript_agency_zoom_match(transcript, jwt_token=jwt_token)
    if match:
        customer_id = match.get("id")
        customer_type = match.get("type") or "customer"
        customer_name = match.get("name") or customer_name

    for task_line in normalized_tasks.splitlines():
        task_title = task_line.strip()
        if not task_title:
            continue

        response_data = create_agency_zoom_task(
            title=task_title,
            due_date=due_datetime,
            task_datetime=due_datetime,
            comments=comments,
            time_specific=False,
            customer_name=customer_name,
            customer_id=customer_id,
            customer_type=customer_type,
            assignee_id=assignee_id,
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
def create_agency_zoom_customer_note_for_transcript(transcript: Any) -> Dict[str, Any]:
    note = _clean_string(getattr(transcript, "crm_note", None))
    if not note:
        raise ValueError("crm_note is required to create an Agency Zoom customer note.")

    jwt_token = zomm_agency_login()
    match = resolve_transcript_agency_zoom_match(transcript, jwt_token=jwt_token)
    if not match or not match.get("id"):
        raise ValueError(
            "No Agency Zoom customer or lead matched this call. Use \"Find in Agency Zoom\" on the "
            "transcript page to link the right record, then approve again."
        )

    return create_agency_zoom_customer_note(customer_id=match["id"], note=note, jwt_token=jwt_token)

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



