# Global variable to store request bodies for the current processing session
_current_request_bodies = None

def set_request_bodies(request_bodies):
    """Set the request bodies for the current processing session"""
    global _current_request_bodies
    _current_request_bodies = request_bodies

def get_request_bodies():
    """Get the request bodies for the current processing session"""
    global _current_request_bodies
    return _current_request_bodies