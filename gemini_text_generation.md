The Gemini API can generate text output from text, images, video, and audio
inputs.

Here's a basic example:

### Python

    from google import genai

    client = genai.Client()

    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        input="How does AI work?"
    )
    print(interaction.output_text)



The Google GenAI SDKs provide convenience properties directly
on the returned `Interaction` object to access the model's response.

The most common helper is **`interaction.output_text`** (String), which returns
the last text blocks in the model's response. If the response is split
across multiple consecutive `TextContent` blocks, it automatically joins them.
Note that `.output_text` does not include earlier text blocks separated by
non-text content (such as thoughts, images, audio, or tool calls). For complex
or interleaved multimodal responses, you must manually iterate over `steps`
instead. To learn more about other media convenience properties, see the
[Interactions overview](https://ai.google.dev/gemini-api/docs/interactions#convenience-properties).

## Thinking with Gemini

Gemini models often have ["thinking"](https://ai.google.dev/gemini-api/docs/interactions/thinking)
enabled by default which allows the model to reason before responding to a
request.

Each model supports different thinking configurations which gives you control
over cost, latency, and intelligence. For more details, see the
[thinking guide](https://ai.google.dev/gemini-api/docs/interactions/thinking#set-budget).

### Python

    from google import genai

    client = genai.Client()

    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        input="How does AI work?",
        generation_config={
            "thinking_level": "low"
        }
    )
    print(interaction.output_text)



## System instructions and other configurations

You can guide the behavior of Gemini models with system instructions. Pass
a `system_instruction` parameter to configure the model's behavior.

### Python

    from google import genai

    client = genai.Client()

    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        system_instruction="You are a cat. Your name is Neko.",
        input="Hello there"
    )

    print(interaction.output_text)


You can also override default generation parameters, such as
temperature, using the `generation_config` parameter.

### Python

    from google import genai

    client = genai.Client()

    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        input="Explain how AI works",
        generation_config={
            "temperature": 1.0
        }
    )
    print(interaction.output_text)



Refer to the [Interactions API reference](https://ai.google.dev/api/interactions-api)
for a complete list of configurable parameters and their
descriptions.

## Multimodal inputs

The Gemini API supports multimodal inputs, allowing you to combine text with
media files. The following example demonstrates providing an image:

### Python

    from google import genai

    client = genai.Client()

    uploaded_file = client.files.upload(file="path/to/organ.jpg")

    interaction = client.interactions.create(
        model="gemini-3.7-flash",
        input=[
            {"type": "text", "text": "Tell me about this instrument"},
            {
                "type": "image",
                "uri": uploaded_file.uri,
                "mime_type": uploaded_file.mime_type
            }
        ]
    )
    print(interaction.output_text)



For alternative methods of providing images and more advanced image processing,
see our [image understanding guide](https://ai.google.dev/gemini-api/docs/interactions/image-understanding).
The API also supports [document](https://ai.google.dev/gemini-api/docs/interactions/document-processing), [video](https://ai.google.dev/gemini-api/docs/interactions/video-understanding), and
[audio](https://ai.google.dev/gemini-api/docs/interactions/audio) inputs and understanding.

## Streaming responses

By default, the model returns a response only after the entire generation
process is complete.

For more fluid interactions, use streaming to handle response chunks
as they're generated. For a comprehensive guide covering event types,
streaming with tools, thinking, agents, and image generation, see the
dedicated [Streaming interactions](https://ai.google.dev/gemini-api/docs/interactions/streaming)
guide.

### Python

    from google import genai

    client = genai.Client()

    stream = client.interactions.create(
        model="gemini-3.7-flash",
        input="Explain how AI works",
        stream=True
    )
    for event in stream:
        if event.event_type == "step.delta":
            if event.delta.type == "text":
                print(event.delta.text, end="")



## Multi-turn conversations

The Interactions API supports multi-turn conversations by chaining interactions
together using `previous_interaction_id`. Each turn is a separate interaction,
and the API automatically manages conversation history.

> [!NOTE]
> **Note:** Unlike other APIs where you might manage conversation history manually, the Interactions API handles conversation state server-side. You pass the `id` from the previous interaction to continue the conversation.

### Python

    from google import genai

    client = genai.Client()

    interaction1 = client.interactions.create(
        model="gemini-3.7-flash",
        input="I have 2 dogs in my house.",
    )
    print(interaction1.output_text)

    interaction2 = client.interactions.create(
        model="gemini-3.7-flash",
        input="How many paws are in my house?",
        previous_interaction_id=interaction1.id,
    )
    print(interaction2.output_text)



Streaming can also be used for multi-turn conversations by combining
`previous_interaction_id` with the streaming methods.

### Python

    from google import genai

    client = genai.Client()

    interaction1 = client.interactions.create(
        model="gemini-3.7-flash",
        input="I have 2 dogs in my house.",
    )
    print(interaction1.output_text)

    stream = client.interactions.create(
        model="gemini-3.7-flash",
        input="How many paws are in my house?",
        previous_interaction_id=interaction1.id,
        stream=True
    )
    for event in stream:
        if event.event_type == "step.delta":
            if event.delta.type == "text":
                print(event.delta.text, end="")



## Stateless conversations

By default, the Interactions API manages conversation state server-side when you use `previous_interaction_id`. However, you can also operate in stateless mode by managing the conversation history yourself on the client side.

To use stateless mode:
1. Set `store=false` in your request to opt out of server-side storage.
2. Maintain the conversation history as an array of **steps** on the client side.
3. In subsequent requests, pass the accumulated steps in the `input` field, and append your new turn as a `user_input` step.

> [!NOTE]
> **Note:** If the model uses "thinking" or tools, you **must** preserve and resend all model-generated steps (such as `thought` and `function_call` steps) exactly as received, as they contain signatures required to continue the conversation.

### Python

    from google import genai

    client = genai.Client()

    history = [
        {
            "type": "user_input",
            "content": [{"type": "text", "text": "I have 2 dogs in my house."}]
        }
    ]

    interaction1 = client.interactions.create(
        model="gemini-3.7-flash",
        store=False,
        input=history
    )
    print("Response 1:", interaction1.steps[-1].content[0].text)

    for step in interaction1.steps:
        history.append(step.model_dump())

    history.append({
        "type": "user_input",
        "content": [{"type": "text", "text": "How many paws are in my house?"}]
    })

    interaction2 = client.interactions.create(
        model="gemini-3.7-flash",
        store=False,
        input=history
    )
    print("Response 2:", interaction2.steps[-1].content[0].text)