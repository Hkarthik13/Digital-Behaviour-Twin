try:
    from flask_bcrypt import Bcrypt # type: ignore
    bcrypt = Bcrypt()
except ImportError:
    # Fallback if flask_bcrypt is not installed
    import bcrypt as _bcrypt
    
    class Bcrypt:
        @staticmethod
        def generate_password_hash(password):
            return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt())
        
        @staticmethod
        def check_password_hash(hashed, password):
            return _bcrypt.checkpw(password.encode('utf-8'), hashed if isinstance(hashed, bytes) else hashed.encode('utf-8'))

bcrypt = Bcrypt()

def hash_password(password):
    return bcrypt.generate_password_hash(password).decode('utf-8')

def check_password(hashed, password):
    return bcrypt.check_password_hash(hashed, password)
