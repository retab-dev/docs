```mermaid
graph TD
    A["JSON schema / Pydantic model"] --> B["UiForm Schema instance"]
    B --> C1["self.inference_json_schema"]
    B --> C2["self.inference_pydantic_model"]
    B --> C3["self.system_prompt"]
    
    C1 --> D["LLM-ready_response_format"]
    C2 --> D
    C3 --> E["LLM-ready messages"]
    
    D --> F
    
    E --> F["OpenAI / Anthropic / Gemini client"]
    subgraph "authoring view"
        B
        C1
        C2
        C3
    end
    
    subgraph "inference view"
        D
        E
        F
    end
    
    %% Description text
    classDef invisible fill:none,stroke:none;
    class description invisible;
```

**Authoring view** - what you give: a concise JSON Schema or Pydantic model describing the payload you want to extract.

**Inference view** - what the LLM needs: the same schema plus helper reasoning fields plus a monster system prompt that teaches the model how to fill them.

Schema is the bridge that keeps those two views in sync.
