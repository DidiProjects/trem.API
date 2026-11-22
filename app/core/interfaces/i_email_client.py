from abc import ABC, abstractmethod


class IEmailClient(ABC):

    @abstractmethod
    async def send_feedback(
        self,
        feedback_type: str,
        message: str,
        user_email: str | None = None,
    ) -> None:
        ...
