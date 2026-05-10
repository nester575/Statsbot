"""Single Flask app instance shared by web_dashboard and web_admin.

Lives in its own module so importing it doesn't drag in route definitions
(which would create circular imports between the route modules).
"""
from flask import Flask

app = Flask(__name__, template_folder="templates")
