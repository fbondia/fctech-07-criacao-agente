from aurora.api import create_app
from tests.fake_model import ScriptedModel


def create_test_app():
    return create_app(model=ScriptedModel())
