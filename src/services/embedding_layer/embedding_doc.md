ChromaDBConnector
A Python class that provides a comprehensive interface for working with ChromaDB, a vector database for storing and retrieving embeddings.

Overview
ChromaDBConnector facilitates the storage, retrieval, and management of vector embeddings for text data. It abstracts away many of the complexities involved in working with embedding models and vector databases, providing a simple API for common operations.

Features
Embedding Generation: Uses Hugging Face models for generating embeddings
Flexible Storage Options: Store text, CSV data, code files, and more
Advanced Retrieval: Similarity search with filtering, scoring, and context management
HTML Context Handling: Specialized methods for working with HTML elements
Configurable: Customizable embedding models and similarity thresholds
Installation
Requirements
Python 3.7+
Required packages can be installed with pip:
pip install langchain langchain-community langchain-chroma chromadb numpy scikit-learn boto3 python-dotenv
Environment Variables
Create a .env file with the following variables:

embedding_model_name=<model_name>
embedding_model_path=<path_to_model>
external_model_threshold=<threshold_value>
default_model_threshold=<default_threshold_value>
RETRIEVAL_CONTEXT=<path_for_retrieval_context>
Usage Examples
Initialization
from chromadb_connector import ChromaDBConnector

# Initialize with a directory to store the vector database
connector = ChromaDBConnector(persist_directory="path/to/store/db")
Storing Data
# Store a CSV file
num_docs = connector.vectordb_store_doc("data.csv")
print(f"Stored {num_docs} documents")

# Store code files from a directory
connector.vectordb_store_code_dir("src_code_path")

# Store text with metadata
texts = ["This is document 1", "This is document 2"]
metadata = [{"source": "manual", "type": "text"}, {"source": "manual", "type": "text"}]
ids = ["doc1", "doc2"]
connector.text_store(data=texts, metadata=metadata, ids=ids)
Retrieving Data
# Basic similarity search
results = connector.retrieval("search query", k=5)

# Similarity search with context storage
context, docs_with_scores, threshold = connector.retrieval_context("search query", k=5)

# Filtered search
filters = {"source": "manual"}
filtered_results = connector.retrieve_filtered("search query", k=5, filters=filters)

# Get document by ID
doc = connector.get_doc_by_id("doc1")
Context Management
# Store context with a key
context_data = {"information": "Important context data"}
connector.store_context_in_chromadb(context_data, "context_key_1")

# Retrieve context by key
retrieved_context = connector.get_context_by_id("context_key_1")

# Update existing context
updated_context = {"information": "Updated context data"}
connector.update_context_in_chromadb(updated_context, "context_key_1")

# Delete context
connector.delete_context_by_id("context_key_1")
HTML Context Retrieval
# Retrieve HTML elements related to a query
filtered_elements, docs, threshold = connector.retrieval_html_context(
    query="search button",
    context_key="webpage_context"
)
Key Classes and Methods
ChromaDBConnector
Constructor

__init__(persist_directory, config_file_path='../Config/config.properties'): Initializes the connector
Configuration

load_config(): Loads configuration including embedding model settings
Storage Methods

text_store(data, metadata, ids): Store text data with metadata
vectordb_store_dir(data_path): Store CSV files from a directory
vectordb_store_doc(data_path): Store a single CSV file
vectordb_store_code(code, f_name): Store code with file name
vectordb_store_code_dir(data_path): Store Python code files from a directory
vector_store(fpath): Store CSV data
embed_csv_with_metadata(fpath, metadata): Store CSV with custom metadata
Retrieval Methods

retrieval(query, k): Basic similarity search
retrieval_context(query, k): Similarity search with context storage
retrieve_filtered(query, k, filters): Filtered similarity search
retrieve_filtered_with_score(query, k, filters): Filtered search with scores
retrieval_html_context(query, context_key): HTML context retrieval
Context Management Methods

get_context_by_id(context_key): Retrieve context by key
update_context_in_chromadb(context, context_key): Update context
store_context_in_chromadb(context, context_key): Store context
delete_context_by_id(context_key): Delete context by key
Utility Methods

get_docs(): Get all documents
get_doc_by_id(id): Get document by ID
decode_json_objects(json_string): Parse multiple JSON objects
filter_elements_with_similarity(context, query, top_k=20): Filter elements by similarity
Technical Details
Vector Similarity
The connector uses cosine similarity for comparing embeddings, configured with "hnsw:space": "cosine" in ChromaDB collections.

Embedding Models
By default, the connector attempts to use a local Hugging Face model specified in the environment variables. If loading fails, it falls back to a default Hugging Face model.

Persistence
All modifications to the ChromaDB collections are persisted to disk using db.persist() to ensure data durability.

Error Handling
The connector includes basic error handling for model loading and directory creation. Operations that might fail should be wrapped in try/except blocks by the calling code.

Limitations
Limited input validation
Uses print statements instead of a proper logging framework
Some operations lack comprehensive error handling
No explicit connection management or cleanup