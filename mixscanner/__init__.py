"""Mix Scanner : identifie les morceaux d'un mix DJ dans l'ordre chronologique."""
import os

# Le Python de python.org sur macOS n'utilise pas les certificats du système :
# sans ça, les connexions HTTPS (Shazam…) échouent avec CERTIFICATE_VERIFY_FAILED.
if not os.environ.get("SSL_CERT_FILE"):
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except ImportError:
        pass
