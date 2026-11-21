from actionflow.api.app import create_app
from actionflow.config import Settings

app = create_app(Settings.from_env())
