import logging
import re
from typing import Generator
        
def api_methods(http_session, method="post"):
    if method not in ["post", "delete", "head", "options", "patch"]:
        raise ValueError("Invalid request, method must be in ['post', 'delete', 'head', 'options', 'patch']")
    elif method == "post":
        return http_session.post
    elif method == "delete":
        return http_session.delete
    elif method == "head":
        return http_session.head
    elif method == "options":
        return http_session.options
    elif method == "patch":
        return http_session.patch

def task_id_generator_function() -> Generator[int, None, None]:
    task_id = 0
    while True:
        yield task_id
        task_id += 1
        
def api_endpoint_from_url(request_url: str) -> str:
    match = re.search(r"^https://[^/]+/v\d+/(.+)$", request_url)
    if match:
        return match.group(1)
    else:
        return ""
    
def api_error(response_json):
    if "error" in response_json:
        logging.warning(
            f"API call failed with error: {response_json['error']}"
            )
        return True
    else:
        return False
    
def rate_limit_error(response_json):
    return "Rate limit" in response_json["error"].get("message", "")