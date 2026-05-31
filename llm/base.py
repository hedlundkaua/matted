from abc import ABC, abstractmethod

class LLMProvider(ABC):
    """
    Abstract Base Class for LLM providers.
    Ensures all providers implement a consistent interface for text and JSON generation.
    """

    @abstractmethod
    def generate(self, prompt: str) -> str:
        """
        Generate a response for the given prompt.
        Should return the raw string output from the LLM.
        """
        pass

    def generate_json(self, prompt: str) -> str:
        """
        Helper method to request JSON output.
        By default, it appends a request for JSON to the prompt.
        Providers can override this for more specific API-level JSON modes.
        """
        json_prompt = f"{prompt}\n\nIMPORTANT: Return ONLY a valid JSON object. Do not include markdown formatting, code blocks, or preamble."
        return self.generate(json_prompt)
