"""All enrichment settings and limits; prices remain provisional in cost/rates.csv."""
MODEL = 'gpt-6-luna'
EFFORT = 'none'
PROMPT_VERSION = 'enrich_v1'
SCHEMA_VERSION = 'v1'
LABEL_CONFIG = f'{MODEL}|effort={EFFORT}|prompt={PROMPT_VERSION}|schema={SCHEMA_VERSION}'
BATCH_SIZE = 25
MAX_BATCH_SIZE = 50
WORKERS = 1
MAX_WORKERS = 8
MAX_API_ATTEMPTS = 3
VALIDATION_RETRIES = 1
SHORT_TEXT_CHARS = 300
MAX_QUOTE_CHARS = 200
MAX_ENTITY_CHARS = 80
MAX_ENTITIES = 20
MAX_OUTPUT_TOKENS = 12000
MAX_INPUT_TOKENS = 100000
INPUT_OVERHEAD_TOKENS = 2048
TIMEOUT_SECONDS = 60
BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 8
DEFAULT_MAX_SPEND_USD = '0'
TOPICS = ('access', 'usability', 'playback', 'downloads', 'catalog', 'billing', 'support', 'other')
INTENTS = ('cancellation', 'complaint', 'request', 'praise', 'unclear')
SENTIMENTS = (-1, -0.5, 0, 0.5, 1)
