from fastapi import FastAPI, Request, Response, BackgroundTasks
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional, Union, Literal
import sys
import os
import json
import asyncio
import threading
import queue
import time
from pathlib import Path
import argparse
import uvicorn
import logging
import numpy as np
import torch
import torch.nn.functional as F
import uuid

# Import from chat_full.py
from chat_full import (
    load_models, 
    initialize_tokenizer, 
    create_unified_state, 
    generate_next_token,
    run_prefill,
    make_causal_mask,
    TokenPrinter,
    parse_args as chat_parse_args
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Hardcoded model directory path
MODEL_DIR = "/example-path/anemll-Meta-Llama-3.2-1B-ctx2048_0.1.2"

app = FastAPI(title="Anemll API Server")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allow all methods
    allow_headers=["*"],  # Allow all headers
)

# Global variables to store model components
embed_model = None
ffn_models = None
lmhead_model = None
tokenizer = None
metadata = {}
state = None
causal_mask = None

# Pydantic models for API
class Message(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[Message]
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    max_tokens: Optional[int] = 1000
    stream: Optional[bool] = False

class ChatCompletionChoice(BaseModel):
    index: int
    message: Optional[Message] = None
    delta: Optional[Dict[str, str]] = None
    finish_reason: Optional[str] = None

class ChatCompletionResponse(BaseModel):
    id: str
    object: str
    created: int
    model: str
    choices: List[ChatCompletionChoice]

class StreamingTokenGenerator:
    """Handles streaming of tokens for the API response."""
    def __init__(self, embed_model, ffn_models, lmhead_model, tokenizer, metadata, state, causal_mask, messages, temperature=0.7):
        self.embed_model = embed_model
        self.ffn_models = ffn_models
        self.lmhead_model = lmhead_model
        self.tokenizer = tokenizer
        self.metadata = metadata
        self.state = state
        self.causal_mask = causal_mask
        self.messages = messages
        self.temperature = temperature
        self.token_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.generation_thread = None
        self.context_length = metadata['context_length']
        self.batch_size = metadata['batch_size']
        self.queue_lock = threading.Lock()  # Add a lock for thread safety

    def start_generation(self):
        """Start token generation in a separate thread."""
        self.generation_thread = threading.Thread(target=self._generate_tokens)
        self.generation_thread.daemon = True
        self.generation_thread.start()

    def _generate_tokens(self):
        """Generate tokens and put them in the queue."""
        try:
            # Convert Pydantic Message objects to dictionaries for the tokenizer
            conversation = []
            for message in self.messages:
                conversation.append({"role": message.role, "content": message.content})
            
            # Format using chat template with full history
            base_input_ids = self.tokenizer.apply_chat_template(
                conversation,
                return_tensors="pt",
                add_generation_prompt=True
            ).to(torch.int32)
            
            # Convert to tensor with batch dimension
            input_ids = base_input_ids
            
            # Run prefill
            current_pos = input_ids.size(1)
            
            # Pad the input_ids tensor to the full context length
            # This is crucial to allow appending new tokens during generation
            input_ids = F.pad(
                input_ids,
                (0, self.context_length - current_pos),
                value=0
            )
            
            # The MLState object doesn't have an items() method
            state_copy = self.state
            
            logger.info(f"Running prefill with {current_pos} tokens")
            logger.info(f"Input shape: {input_ids.shape}")
            logger.info(f"Batch size: {self.batch_size}")
            
            # Run prefill for the conversation context
            try:
                run_prefill(
                    self.embed_model, 
                    self.ffn_models, 
                    input_ids,
                    current_pos, 
                    self.context_length, 
                    self.batch_size,
                    state_copy, 
                    self.causal_mask
                )
            except RuntimeError as e:
                error_msg = str(e)
                logger.error(f"CoreML model error: {error_msg}")
                
                if "Shape" in error_msg and "was not in enumerated set of allowed shapes" in error_msg:
                    logger.error(f"The model requires specific input shapes. Please ensure the batch_size in meta.yaml ({self.batch_size}) matches what the model expects.")
                    
                # Put error message in queue and exit
                with self.queue_lock:
                    self.token_queue.put({"error": f"Model error: {error_msg}"})
                return
            
            # Generate tokens one by one
            pos = current_pos
            generated_ids = []
            
            logger.info(f"Starting token generation from position {pos}")
            
            while not self.stop_event.is_set():
                # Make sure input_ids has the right shape [1, seq_len]
                if len(input_ids.shape) != 2 or input_ids.shape[0] != 1:
                    logger.info(f"Reshaping input_ids from {input_ids.shape} to [1, seq_len]")
                    input_ids = input_ids.view(1, -1)
                
                next_token_id = generate_next_token(
                    self.embed_model,
                    self.ffn_models,
                    self.lmhead_model,
                    input_ids,  # Already a tensor with shape [1, seq_len]
                    pos,
                    self.context_length,
                    state_copy,
                    self.causal_mask,
                    temperature=self.temperature
                )
                
                # Check for EOS token
                if next_token_id == self.tokenizer.eos_token_id:
                    logger.info(f"EOS token detected (ID: {next_token_id})")
                    self.stop_event.set()  # Set the stop event to signal completion
                    with self.queue_lock:
                        self.token_queue.put(None)  # Signal end of generation
                    break
                
                # Decode the token
                token_text = self.tokenizer.decode([next_token_id])
                with self.queue_lock:
                    self.token_queue.put(token_text)
                
                # Update for next iteration - directly assign the new token to the tensor
                # This matches the original implementation in chat_full.py
                input_ids[0, pos] = next_token_id
                generated_ids.append(next_token_id)
                pos += 1
                
        except Exception as e:
            logger.error(f"Error in token generation: {str(e)}")
            import traceback
            traceback.print_exc()
            with self.queue_lock:
                self.token_queue.put(None)  # Signal end of generation

    def get_tokens(self):
        """Get the next token from the queue."""
        try:
            # Use a lock to ensure thread safety when accessing the queue
            with self.queue_lock:
                # Use a shorter timeout to avoid GIL issues
                token = self.token_queue.get(timeout=0.05)
            return token  # This could be None, a string token, or an error dict
        except queue.Empty:
            return None

    def stop(self):
        """Stop token generation."""
        # Set the stop event to signal the generation thread to stop
        self.stop_event.set()
        
        # Clear the queue to prevent blocking
        try:
            with self.queue_lock:  # Use the lock when clearing the queue
                while not self.token_queue.empty():
                    self.token_queue.get_nowait()
        except Exception:
            pass
            
        # Join the thread with a timeout
        if self.generation_thread and self.generation_thread.is_alive():
            try:
                self.generation_thread.join(timeout=1)
            except Exception as e:
                logger.error(f"Error joining generation thread: {str(e)}")


async def stream_chat_completion(request: ChatCompletionRequest):
    """Stream chat completion response."""
    generator = StreamingTokenGenerator(
        embed_model=embed_model,
        ffn_models=ffn_models,
        lmhead_model=lmhead_model,
        tokenizer=tokenizer,
        metadata=metadata,
        state=state,
        causal_mask=causal_mask,
        messages=request.messages,
        temperature=request.temperature
    )
    
    generator.start_generation()
    
    # Create a unique ID for this completion
    completion_id = f"chatcmpl-{uuid.uuid4()}"
    created_time = int(time.time())
    
    try:
        # Send the initial chunk
        yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created_time, 'model': request.model, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
        
        content_so_far = ""
        
        while True:
            token = await asyncio.get_event_loop().run_in_executor(None, generator.get_tokens)
            
            # Check if token is None (end of generation or timeout)
            if token is None:
                # Check if we're just waiting for more tokens or if the EOS token was detected
                if not generator.stop_event.is_set():
                    # Just a timeout, continue waiting
                    await asyncio.sleep(0.1)  # Add a small delay to prevent CPU spinning
                    continue
                
                # End of generation (EOS token was detected or generation was stopped)
                logger.info("End of generation detected (EOS token or stop event)")
                yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created_time, 'model': request.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"
                break
            
            # Check if token is an error message
            if isinstance(token, dict) and "error" in token:
                error_msg = token["error"]
                yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created_time, 'model': request.model, 'choices': [{'index': 0, 'delta': {'content': f'Error: {error_msg}'}, 'finish_reason': 'error'}]})}\n\n"
                yield "data: [DONE]\n\n"
                break
            
            content_so_far += token
            
            # Send the token
            yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created_time, 'model': request.model, 'choices': [{'index': 0, 'delta': {'content': token}, 'finish_reason': None}]})}\n\n"
    except Exception as e:
        logger.error(f"Error in streaming: {str(e)}")
        yield f"data: {json.dumps({'id': completion_id, 'object': 'chat.completion.chunk', 'created': created_time, 'model': request.model, 'choices': [{'index': 0, 'delta': {'content': f'Error: {str(e)}'}, 'finish_reason': 'error'}]})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        try:
            # Make sure to stop the generator in a way that doesn't block
            await asyncio.get_event_loop().run_in_executor(None, generator.stop)
        except Exception as e:
            logger.error(f"Error stopping generator: {str(e)}")

async def generate_chat_completion(request: ChatCompletionRequest):
    """Generate non-streaming chat completion response."""
    generator = StreamingTokenGenerator(
        embed_model=embed_model,
        ffn_models=ffn_models,
        lmhead_model=lmhead_model,
        tokenizer=tokenizer,
        metadata=metadata,
        state=state,
        causal_mask=causal_mask,
        messages=request.messages,
        temperature=request.temperature
    )
    
    try:
        # Start token generation
        generator.start_generation()
        
        # Collect all tokens
        full_content = ""
        while True:
            token = await asyncio.get_event_loop().run_in_executor(None, generator.get_tokens)
            if token is None:
                if generator.stop_event.is_set():
                    break
                await asyncio.sleep(0.05)
                continue
            
            if isinstance(token, dict) and "error" in token:
                raise Exception(token["error"])
                
            full_content += token
    finally:
        try:
            # Make sure to stop the generator in a way that doesn't block
            await asyncio.get_event_loop().run_in_executor(None, generator.stop)
        except Exception as e:
            logger.error(f"Error stopping generator: {str(e)}")
    
    # Create response
    response_id = f"chatcmpl-{uuid.uuid4()}"
    return {
        'id': response_id,
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': request.model,
        'choices': [{
            'index': 0,
            'message': {
                'role': 'assistant',
                'content': full_content
            },
            'finish_reason': 'stop'
        }]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """OpenAI-compatible chat completions endpoint."""
    if request.stream:
        return StreamingResponse(
            stream_chat_completion(request),
            media_type="text/event-stream"
        )
    else:
        return await generate_chat_completion(request)


@app.get("/v1/models")
@app.options("/v1/models")
async def list_models_v1():
    """List available models (v1 endpoint) - required for Open WebUI compatibility."""
    model_name = "anemll-model"
    
    # Extract model name from the directory path if possible
    try:
        model_dir = Path(MODEL_DIR)
        if model_dir.name.startswith("anemll-"):
            model_name = model_dir.name
    except:
        pass
    
    return {
        "object": "list",
        "data": [
            {
                "id": model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "anemll",
                "permission": [],
                "root": model_name,
                "parent": None
            }
        ]
    }

def load_model_components():
    """Load all model components."""
    global embed_model, ffn_models, lmhead_model, tokenizer, metadata, state, causal_mask
    
    # Convert directory to absolute path
    model_dir = Path(MODEL_DIR).resolve()
    if not model_dir.exists():
        logger.error(f"Model directory not found: {model_dir}")
        sys.exit(1)
        
    logger.info(f"Using model directory: {model_dir}")
    
    # Load meta.yaml directly to get the exact parameters
    meta_yaml_path = model_dir / "meta.yaml"
    if not meta_yaml_path.exists():
        logger.error(f"meta.yaml not found at {meta_yaml_path}")
        sys.exit(1)
        
    try:
        import yaml
        with open(meta_yaml_path, 'r') as f:
            meta_yaml = yaml.safe_load(f)
            
        # Extract parameters from meta.yaml
        params = meta_yaml['model_info']['parameters']
        logger.info(f"Loaded parameters from meta.yaml: {params}")
        
        # Use the model prefix from meta.yaml
        prefix = params.get('model_prefix', 'llama')
        
        # Use the correct file names based on meta.yaml
        embed_path = str(model_dir / f"{prefix}_embeddings.mlmodelc")
        
        # Handle chunked FFN models
        num_chunks = int(params['num_chunks'])
        lut_ffn = params['lut_ffn']
        lut_suffix = f"_lut{lut_ffn}" if lut_ffn != 'none' else ''
        
        # For the first chunk
        ffn_path = str(model_dir / f"{prefix}_FFN_PF{lut_suffix}_chunk_01of{num_chunks:02d}.mlmodelc")
        
        # LM head path
        lut_lmhead = params['lut_lmhead']
        lmhead_suffix = f"_lut{lut_lmhead}" if lut_lmhead != 'none' else ''
        lmhead_path = str(model_dir / f"{prefix}_lm_head{lmhead_suffix}.mlmodelc")
        
        tokenizer_path = str(model_dir)
    except Exception as e:
        logger.error(f"Error loading meta.yaml: {str(e)}")
        sys.exit(1)
    
    # Create a chat_args object that matches what load_models expects
    chat_args = argparse.Namespace()
    chat_args.embed = embed_path
    chat_args.ffn = ffn_path
    chat_args.lmhead = lmhead_path
    chat_args.tokenizer = tokenizer_path
    chat_args.context_length = int(params['context_length'])  # Use context_length from meta.yaml
    chat_args.batch_size = int(params['batch_size'])  # Use batch_size from meta.yaml
    chat_args.d = MODEL_DIR
    chat_args.meta = str(meta_yaml_path)  # Pass the meta.yaml path to load_models
    
    # Load models and extract metadata
    try:
        embed_model, ffn_models, lmhead_model, metadata = load_models(chat_args, {})
    except Exception as e:
        logger.error(f"Error loading models: {str(e)}")
        
        # Print more detailed error information
        logger.error("\nPlease ensure all model files exist and are accessible.")
        logger.error(f"Expected files:")
        logger.error(f"  Embeddings: {embed_path}")
        logger.error(f"  LM Head: {lmhead_path}")
        logger.error(f"  FFN: {ffn_path}")
        
        # Check if files exist
        logger.error("\nChecking if files exist:")
        logger.error(f"  Embeddings exists: {os.path.exists(embed_path)}")
        logger.error(f"  LM Head exists: {os.path.exists(lmhead_path)}")
        logger.error(f"  FFN exists: {os.path.exists(ffn_path)}")
        
        # List files in the directory
        logger.error("\nFiles in model directory:")
        for file in os.listdir(model_dir):
            logger.error(f"  {file}")
        
        sys.exit(1)
    
    # Log the metadata values without modifying them
    logger.info(f"Using context length: {metadata['context_length']}")
    logger.info(f"Using batch size: {metadata['batch_size']}")
    logger.info(f"Using state length: {metadata['state_length']}")
    
    logger.info(f"Metadata: {metadata}")
    
    # Load tokenizer
    tokenizer = initialize_tokenizer(tokenizer_path)
    if tokenizer is None:
        logger.error("Failed to initialize tokenizer")
        sys.exit(1)
    
    # Create unified state
    state = create_unified_state(ffn_models, metadata['context_length'])
    
    # Initialize causal mask
    causal_mask = make_causal_mask(metadata['context_length'], 0)
    causal_mask = torch.tensor(causal_mask, dtype=torch.float16)
    # causal_mask = initialize_causal_mask()
    
    logger.info("Model components loaded successfully")

def main():
    """Main function to start the server."""
    # Load model components
    load_model_components()
    
    # Start the server
    host = "0.0.0.0"
    port = 8000
    logger.info(f"Starting server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)

if __name__ == "__main__":
    main() 