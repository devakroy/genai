import configparser
from typing import List
import json
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
import os


os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["CHROMA_DB_TELEMETRY"] = "False"

# Add this right after setting the environment variable
import chromadb
chromadb.config.Settings.anonymized_telemetry = False
from langchain_chroma import Chroma
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.document_loaders.csv_loader import CSVLoader
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_core.documents import Document
import os
from datetime import datetime, timedelta
import logging
from dotenv import load_dotenv
from utils.constants import PLATFORM_CONFIG
logger = logging.getLogger(__name__)
load_dotenv(".env")


class ChromaDBConnector:

    def __init__(self):
        base = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(os.path.dirname(base))
        parent = os.path.dirname(root)
        self.custom_temp_dir = os.path.join(parent, PLATFORM_CONFIG.get("CHROMADB_EMBEDDING_PATH"))
        self.persist_directory = os.path.join(parent, PLATFORM_CONFIG.get("CHROMADB_STORAGE_PATH"))
        os.makedirs(self.persist_directory, exist_ok=True)
        self.context = ''
        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.load_config()

    def cleanup_old_contexts(self, hours_threshold=2):
        """
        Delete contexts older than specified hours
        """
        try:
            # Check if ChromaDB directory exists and has any data
            if not os.path.exists(self.persist_directory):
                logger.info("ChromaDB directory doesn't exist yet, no cleanup needed")
                return 0

            # Check if there are any .sqlite files (ChromaDB creates these)
            sqlite_files = [f for f in os.listdir(self.persist_directory) if f.endswith('.sqlite3')]
            if not sqlite_files:
                logger.info("No ChromaDB data files found, no cleanup needed")
                return 0

            db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                        collection_metadata={"hnsw:space": "cosine"})

            all_docs = db.get()

            if not all_docs['ids']:
                logger.info("ChromaDB is empty, no cleanup needed")
                return 0

            current_time = datetime.now()
            cutoff_time = current_time - timedelta(hours=hours_threshold)
            ids_to_delete = []
            contexts_checked = 0

            logger.info(f"Cleanup threshold: {cutoff_time} ({hours_threshold} hours ago)")

            for i, metadata in enumerate(all_docs['metadatas'] or []):
                if metadata and 'context_key' in metadata:
                    contexts_checked += 1
                    context_key = metadata['context_key']

                    # Get timestamp
                    doc_time = None
                    if 'timestamp' in metadata:
                        try:
                            doc_time = datetime.fromisoformat(metadata['timestamp'])
                        except ValueError:
                            logger.warning(f"Invalid timestamp in context '{context_key}'")

                    # If no valid timestamp, check file creation time as fallback
                    if doc_time is None:
                        logger.info(f"No valid timestamp for '{context_key}', assuming it's old")
                        doc_time = cutoff_time - timedelta(hours=1)  # Force deletion

                    if doc_time < cutoff_time:
                        ids_to_delete.append(all_docs['ids'][i])
                        age_hours = (current_time - doc_time).total_seconds() / 3600
                        logger.debug(f"Marking for deletion: '{context_key}' (age: {age_hours:.1f} hours)")

            # Perform deletion
            deleted_count = 0
            if ids_to_delete:
                db.delete(ids=ids_to_delete)
                deleted_count = len(ids_to_delete)
                logger.info(f"🗑️ Deleted {deleted_count}/{contexts_checked} contexts (older than {hours_threshold}h)")
            else:
                pass

            return deleted_count

        except Exception as e:
            logger.error(f"❌ Error during context cleanup: {e}")
            # Don't fail initialization due to cleanup errors
            return -1

    def load_config(self):
        self.embeddings = HuggingFaceEmbeddings(
            cache_folder=self.custom_temp_dir,  # ← Only this line added
            encode_kwargs={'normalize_embeddings': True}
        )
        self.threshold = 0.50

    def text_store(self, data, metadata, ids):

        client = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self.embeddings,
            collection_metadata={"hnsw:space": "cosine"}
        )
        client.add_texts(data, metadatas=metadata, ids=ids)  # Changed 'metadata' to 'metadatas'
        # client.persist()

    def vectordb_store_dir(self, data_path):
        loader = DirectoryLoader(data_path, glob="**/*.csv", loader_cls=CSVLoader)
        data = loader.load()

        # load it into Chroma
        db = Chroma.from_documents(documents=data, embedding=self.embeddings,
                                   ids=[doc.metadata['source'].split("\\")[-1].split(".")[0] for doc in data],
                                   persist_directory=self.persist_directory,
                                   collection_metadata={"hnsw:space": "cosine"})
        db.persist()

    def vectordb_store_doc(self, data_path):
        loader = CSVLoader(data_path)
        data = loader.load()
        # logger.info(data)
        # load it into Chroma
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        db.persist()
        ids = db.add_documents(documents=data)
        db.persist()
        return len(ids)

    def vectordb_store_code(self, code, f_name):
        data = Document(page_content=code)

        # load it into Chroma
        db = Chroma.from_documents(documents=[data], embedding=self.embeddings,
                                   persist_directory=self.persist_directory, ids=[f_name],
                                   collection_metadata={"hnsw:space": "cosine"})
        db.persist()

    def vectordb_store_code_dir(self, data_path):
        loader = DirectoryLoader(data_path, glob="**/*.py", loader_cls=TextLoader)
        data = loader.load()

        # load it into Chroma
        db = Chroma.from_documents(documents=data, embedding=self.embeddings,
                                   ids=[doc.metadata['source'].split("\\")[-1].split(".")[0] for doc in data],
                                   persist_directory=self.persist_directory,
                                   collection_metadata={"hnsw:space": "cosine"})
        db.persist()

    def retrieval(self, query, k):
        db = Chroma(
            persist_directory=self.persist_directory,
            embedding_function=self.embeddings,
            collection_metadata={"hnsw:space": "cosine"}
        )
        retrieved_docs = db.similarity_search(query, k=k)
        return retrieved_docs


    def get_docs(self):
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        return db.get()

    def get_doc_by_id(self, id):
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        return db.get(ids=[id])

    def embed_csv_with_metadata(self, fpath, metadata):

        embedding_path = self.persist_directory

        try:
            os.makedirs(embedding_path, exist_ok=True)
        except OSError as e:
            logger.info(f"Error creating directory: {e}")

        # load the json data
        # loader = DataFrameLoader(df, page_content_column=df.columns)
        loader = CSVLoader(file_path=fpath, encoding="utf-8")
        documents = loader.load()

        # add metadata
        for document in documents:
            document.metadata = metadata

        # load it into Chroma
        db = Chroma.from_documents(documents, self.embeddings, persist_directory=embedding_path,
                                   collection_metadata={"hnsw:space": "cosine"})
        db.persist()

    def vector_store(self, fpath):

        embedding_path = self.persist_directory

        try:
            os.makedirs(embedding_path, exist_ok=True)
        except OSError as e:
            logger.info(f"Error creating directory: {e}")

        # load the json data
        # loader = DataFrameLoader(df, page_content_column=df.columns)
        loader = CSVLoader(file_path=fpath, encoding="utf-8")
        documents = loader.load()

        # load it into Chroma
        db = Chroma.from_documents(documents, self.embeddings, persist_directory=embedding_path,
                                   collection_metadata={"hnsw:space": "cosine"})
        db.persist()

    def get(self):
        return self.get_docs()

    def retrieve_filtered(self, query: str, k: int, filters: dict) -> List[Document]:
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        retrieved_docs = db.similarity_search(query, k=k, filter=filters)
        return retrieved_docs

    def retrieve_filtered_with_score(self, query: str, k: int, filters: dict):
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        retrieved_docs = db.similarity_search_with_score(query, k=k, filter=filters)
        return retrieved_docs

    def decode_json_objects(self, json_string):
        json_objects = json_string.split('\n')
        decoded_objects = []
        for obj in json_objects:
            if obj.strip():  # Skip empty lines
                decoded_objects.append(json.loads(obj))
        return decoded_objects

    def get_context_by_id(self, context_key):
        """
        Retrieve the context for a given key from ChromaDB.
        """
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        filters = {"context_key": context_key}
        results = db.get(where=filters)
        if results['documents']:
            # Assuming the first document is the one we want
            doc = results['documents'][0]
            return json.loads(doc)  # Convert JSON string back to dictionary
        return None

    def update_context_in_chromadb(self, context, context_key):
        """
        Update the context for a given key in ChromaDB.
        """
        # Retrieve the existing context
        existing_context = self.get_context_by_id(context_key)
        if existing_context:
            # Compare existing context with the new context
            if existing_context != json.dumps(context):
                # Delete the existing context
                self.delete_context_by_id(context_key)
                # Store the new context
                self.store_context_in_chromadb(context, context_key)
                # logger.info(f"Updated context for key: {context_key}")
            else:
                logger.info(f"Context for key: {context_key} is already up-to-date.")
        else:
            # If no existing context, store the new context
            self.store_context_in_chromadb(context, context_key)
            logger.info(f"Stored new context for key: {context_key}")

    def store_context_in_chromadb(self, context, context_key):
        """
        Store the context for a given key in ChromaDB.
        The context key should be unique to avoid overwriting existing data.
        """
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        # Convert context dictionary to JSON string to store as a single document
        context_json = json.dumps(context)
        metadata = {
            "context_key": context_key,
            "timestamp": datetime.now().isoformat(),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "context_type": "html_elements"
        }
        db.add_texts([context_json], metadatas=[metadata], ids=[context_key])

    def delete_context_by_id(self, context_key):
        """
        Delete context by its key.
        """
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        db.delete(ids=[context_key])
        # db.persist() # Persist changes
        # logger.info(f"Deleted context for key: {context_key}")

    def retrieval_html_context(self, query, context_key):
        db = Chroma(persist_directory=self.persist_directory, embedding_function=self.embeddings,
                    collection_metadata={"hnsw:space": "cosine"})
        filters = {"context_key": context_key}
        docs = db.get(where=filters)
        docs_with_similarity = {}
        self.context = ""
        for doc in docs['documents']:
            # logger.info(score, doc)
            docs_with_similarity = doc

        retrieved_docs = db.get(where=filters)
        for doc in retrieved_docs['documents']:
            content = doc
            self.context += (content + '\n')
        # Debugging: logger.info the retrieved context
        # logger.info("Retrieved context:", self.context)

        # Decode JSON objects if necessary
        try:
            filtered_elements = self.decode_json_objects(self.context)
        except json.JSONDecodeError as e:
            logger.info(f"JSON decode error: {e}")
            return None, None, None

        # Filter elements based on the query using cosine similarity
        filtered_elements = self.filter_elements_with_similarity(filtered_elements, query)

        return json.dumps(filtered_elements), docs_with_similarity, self.threshold

    def filter_elements_with_similarity(self, context, query, top_k=10):
        query_embedding = self.embeddings.embed_query(query)
        similarities = []
        unique_elements = set()
        elements_to_process = []

        if context and isinstance(context, list) and len(context) > 0:
            if isinstance(context[0], list):
                # It's a list of lists of dicts
                elements_to_process = [item for sublist in context for item in sublist]
            else:
                # Assume it's a flat list of dicts
                elements_to_process = context
        else:  # Not a list, empty, or not in expected format
            return []

        # Filter elements with the key "element"
        elements = [item for item in elements_to_process if "element" in item and item["element"]]
        element_texts = [item["element"] for item in elements]
        element_embeddings = self.embeddings.embed_documents(element_texts)

        query_embedding = np.array(query_embedding).reshape(1, -1)
        element_embeddings = np.array(element_embeddings)
        cosine_similarities = cosine_similarity(query_embedding, element_embeddings)[0]

        for idx, item in enumerate(elements):
            unique_id = f"{item['xpath']}-{item['element']}"
            if unique_id not in unique_elements:
                similarities.append((item, cosine_similarities[idx]))
                unique_elements.add(unique_id)

        similarities.sort(key=lambda x: x[1], reverse=True)
        filtered_elements = [element for element, similarity in similarities[:top_k]]

        return filtered_elements

