# chat-pykg/ingest.py
import tempfile
import gradio as gr
from langchain.document_loaders import SitemapLoader, ReadTheDocsLoader, TextLoader
from langchain.embeddings import OpenAIEmbeddings, HuggingFaceEmbeddings
from langchain.text_splitter import RecursiveCharacterTextSplitter, PythonCodeTextSplitter, MarkdownTextSplitter, TextSplitter
from langchain.vectorstores.faiss import FAISS
import os
from langchain.vectorstores import Chroma
import shutil
from pathlib import Path
import subprocess
import chromadb
import magic
from typing import Any, Dict, Iterable, List, Optional, Type, TypeVar
from pydantic import Extra, Field, root_validator
import logging
logger = logging.getLogger()
from langchain.docstore.document import Document
import numpy as np

def embedding_chooser(embedding_radio):
    if embedding_radio == "Sentence Transformers":
        embedding_function = HuggingFaceEmbeddings()
    elif embedding_radio == "OpenAI":
        embedding_function = OpenAIEmbeddings()
    else:
        embedding_function = HuggingFaceEmbeddings()
    return embedding_function

# Monkeypatch pending PR
def _merge_splits(self, splits: Iterable[str], separator: str) -> List[str]:
    # We now want to combine these smaller pieces into medium size
    # chunks to send to the LLM.
    separator_len = self._length_function(separator)

    docs = []
    current_doc: List[str] = []
    total = 0
    for index, d in enumerate(splits):
        _len = self._length_function(d)
        if (
            total + _len + (separator_len if len(current_doc) > 0 else 0)
            > self._chunk_size
        ):
            if total > self._chunk_size:
                logger.warning(
                    f"Created a chunk of size {total}, "
                    f"which is longer than the specified {self._chunk_size}"
                )
            if len(current_doc) > 0:
                doc = self._join_docs(current_doc, separator)
                if doc is not None:
                    docs.append(doc)
                # Keep on popping if:
                # - we have a larger chunk than in the chunk overlap
                # - or if we still have any chunks and the length is long
                while total > self._chunk_overlap or (
                    total + _len + (separator_len if len(current_doc) > 0 else 0)
                    > self._chunk_size
                    and total > 0
                ):
                    total -= self._length_function(current_doc[0]) + (
                        separator_len if len(current_doc) > 1 else 0
                    )
                    current_doc = current_doc[1:]

        if index > 0:
            current_doc.append(separator + d)
        else:
            current_doc.append(d)
        total += _len + (separator_len if len(current_doc) > 1 else 0)
    doc = self._join_docs(current_doc, separator)
    if doc is not None:
        docs.append(doc)
    return docs

def get_text(content):
    relevant_part = content.find("div", {"class": "markdown"})
    if relevant_part is not None:
        return relevant_part.get_text(separator=" ")
    else:
        return ""

def ingest_docs(all_collections_state, urls, chunk_size, chunk_overlap, vectorstore_radio, embedding_radio, debug=False):
    cleared_list = urls.copy()
    def sanitize_folder_name(folder_name):
        if folder_name != '':
            folder_name = folder_name.strip().rstrip('/')
        else:
            folder_name = '.' # current directory
        return folder_name

    def is_hidden(path):
        return os.path.basename(path).startswith('.')
    
    embedding_function = embedding_chooser(embedding_radio)
    all_docs = []
    shutil.rmtree('downloaded/', ignore_errors=True)
    known_exts = ["py", "md"]
    # Initialize text splitters
    py_splitter = PythonCodeTextSplitter(chunk_size=int(chunk_size), chunk_overlap=int(chunk_overlap))
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=int(chunk_size), chunk_overlap=int(chunk_overlap))
    md_splitter = MarkdownTextSplitter(chunk_size=int(chunk_size), chunk_overlap=int(chunk_overlap))
    py_splitter._merge_splits = _merge_splits.__get__(py_splitter, TextSplitter)
    # Process input URLs
    urls = [[url.strip(), [sanitize_folder_name(folder) for folder in url_folders.split(',')]] for url, url_folders in urls]
    for j in range(len(urls)):
        orgrepo = urls[j][0]
        repo_folders = urls[j][1]
        if orgrepo == '':
            continue
        if orgrepo.replace('/','-') in all_collections_state:
            logging.info(f"Skipping {orgrepo} as it is already in the database")
            continue
        documents_split = []
        documents = []
        paths = []
        paths_by_ext = {}
        docs_by_ext = {}
        for ext in known_exts + ["other"]:
            docs_by_ext[ext] = []
            paths_by_ext[ext] = []
        
        if orgrepo[0] == '/' or orgrepo[0] == '.':
            # Ingest local folder
            local_repo_path = sanitize_folder_name(orgrepo[1:])
        else:
            # Ingest remote git repo
            org = orgrepo.split('/')[0]
            repo = orgrepo.split('/')[1]
            repo_url = f"https://github.com/{org}/{repo}.git"
            local_repo_path = os.path.join('.downloaded', orgrepo) if debug else tempfile.mkdtemp()

            # Initialize the Git repository
            subprocess.run(["git", "init"], cwd=local_repo_path)
            # Add the remote repository
            subprocess.run(["git", "remote", "add", "-f", "origin", repo_url], cwd=local_repo_path)
            # Enable sparse-checkout
            subprocess.run(["git", "config", "core.sparseCheckout", "true"], cwd=local_repo_path)
            # Specify the folder to checkout
            cmd = ["git", "sparse-checkout", "set"] + [i for i in repo_folders]
            subprocess.run(cmd, cwd=local_repo_path)
            # Check if branch is called main or master

            # Checkout the desired branch
            res = subprocess.run(["git", "checkout", 'main'], cwd=local_repo_path)
            if res.returncode == 1:
                res = subprocess.run(["git", "checkout", "master"], cwd=local_repo_path)
            #res = subprocess.run(["cp", "-r", (Path(local_repo_path) / repo_folders[i]).as_posix(), '/'.join(destination.split('/')[:-1])])#
            # Iterate through files and process them
        if local_repo_path == '.':
            orgrepo='chat-pykg'
        for root, dirs, files in os.walk(local_repo_path):
            dirs[:] = [d for d in dirs if not is_hidden(d)]  # Ignore hidden directories
            for file in files:
                if is_hidden(file):
                    continue
                file_path = os.path.join(root, file)
                rel_file_path = os.path.relpath(file_path, local_repo_path)
                try:
                    if '.' not in rel_file_path:
                        inferred_filetype = magic.from_file(file_path, mime=True)
                        if "python" in inferred_filetype or "text/plain" in inferred_filetype:
                            ext = "py"
                        else:
                            ext = "other"
                    else:
                        ext = rel_file_path.split('.')[-1]
                    if docs_by_ext.get(ext) is None:
                        ext = "other" 
                    doc = TextLoader(os.path.join(local_repo_path, rel_file_path)).load()[0]
                    doc.metadata["source"] = os.path.join(orgrepo, rel_file_path)
                    docs_by_ext[ext].append(doc)
                    paths_by_ext[ext].append(rel_file_path)
                except Exception as e:
                    continue
        for ext in docs_by_ext.keys():
            if ext == "py":
                documents_split += py_splitter.split_documents(docs_by_ext[ext])
                documents += docs_by_ext[ext]
            if ext == "md":
                documents_split += md_splitter.split_documents(docs_by_ext[ext])
                documents += docs_by_ext[ext]
            # else:
            #     documents += text_splitter.split_documents(docs_by_ext[ext]
        all_docs += documents_split
        # For each document, add the metadata to the page_content
        for doc in documents_split:
            if local_repo_path != '.':
                doc.metadata["source"] = doc.metadata["source"].replace(local_repo_path, "")
            if doc.metadata["source"] == '/':
                doc.metadata["source"] = doc.metadata["source"][1:]
            doc.page_content = f'# source:{doc.metadata["source"]}\n{doc.page_content}'
        for doc in documents:
            if local_repo_path != '.':
                doc.metadata["source"] = doc.metadata["source"].replace(local_repo_path, "")
            if doc.metadata["source"] == '/':
                doc.metadata["source"] = doc.metadata["source"][1:]
            doc.page_content = f'# source:{doc.metadata["source"]}\n{doc.page_content}'
            
        if type(embedding_radio) == gr.Radio:
            embedding_radio = embedding_radio.value
        persist_directory = os.path.join(".persisted_data", embedding_radio.replace(' ','_'))
        persist_directory_raw = Path('.persisted_data_raw')
        persist_directory_raw.mkdir(parents=True, exist_ok=True)
        collection_name = orgrepo.replace('/','-')

        if vectorstore_radio == 'Chroma':
            collection = Chroma.from_documents(documents=documents_split, collection_name=collection_name, embedding=embedding_function, persist_directory=persist_directory)
            collection.persist()
        
        if vectorstore_radio == 'raw':
        # Persist the raw documents
            docarr = np.array([doc.page_content for doc in documents_split])
            np.save(os.path.join(persist_directory_raw, f"{collection_name}.npy"), docarr)
            # with open(os.path.join(persist_directory_raw, f"{collection_name}"), "w") as f:
            #     for doc in documents:
            #         f.write(doc.page_content)

        all_collections_state.append(collection_name)
        cleared_list[j][0], cleared_list[j][1] = '', ''
    return all_collections_state, gr.update(value=cleared_list)

if __name__ == "__main__":
    ingest_docs()
