from abc import ABC, abstractmethod


class IEmailClient(ABC):

    @abstractmethod
    async def send_provisional_password(
        self,
        to: str,
        username: str,
        provisional_password: str,
    ) -> None:
        ...

    @abstractmethod
    async def send_email_verification(self, to: str, username: str, link: str, ttl_hours: int) -> None:
        ...

    @abstractmethod
    async def send_signup_conflict(self, to: str, reason: str, login_url: str) -> None:
        ...

    @abstractmethod
    async def send_signup_pending_approval(self, to: str, username: str, email: str, user_id: str) -> None:
        ...

    @abstractmethod
    async def send_feedback(
        self,
        feedback_type: str,
        message: str,
        user_email: str | None = None,
    ) -> None:
        ...
