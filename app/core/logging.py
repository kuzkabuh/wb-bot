import json, logging, sys

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = {"level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(base, ensure_ascii=False)

def setup_logging(level="INFO"):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
