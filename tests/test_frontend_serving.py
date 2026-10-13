from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app


def test_bundled_frontend_and_assets_are_served_without_hiding_api():
    with TestClient(create_app(Settings())) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert 'text/html' in page.headers['content-type']
        assert 'id="checkout-dialog"' in page.text
        assert client.get('/app.js').status_code == 200
        assert client.get('/styles.css').status_code == 200
        assert client.get('/assets/shirt.svg').status_code == 200
        assert client.get('/health').json()['status'] == 'ok'
        assert client.get('/api/config').json()['amount_paise'] == 100000
        assert client.get('/docs').status_code == 200
        assert client.get('/api/missing-route').status_code == 404
