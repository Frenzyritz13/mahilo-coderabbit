from typing import Any, Dict, List, Optional
from fastapi import WebSocket
from mahilo.agent import BaseAgent
from pydantic_ai import Agent, RunContext
from rich.console import Console

from mahilo.integrations.pydanticai.tools import get_chat_with_agent_tool_pydanticai

console = Console()

class PydanticAIAgent(BaseAgent):
    """Adapter class to use PydanticAI agents within the mahilo framework."""
    
    def __init__(self, 
                 pydantic_agent: Agent,  # The actual PydanticAI agent instance
                 type: str = "pydantic_ai",
                 name: str = None,
                 description: str = None,
                 can_contact: List[str] = [],
                 short_description: str = None):
        """Initialize a PydanticAIAgent.
        
        Args:
            pydantic_agent: The PydanticAI agent instance to wrap
            type: The type of agent
            name: Unique name for this agent instance
            description: Long description of the agent
            can_contact: List of agent types this agent can contact
            short_description: Brief description of the agent
        """
        super().__init__(
            type=type,
            name=name,
            description=description,
            can_contact=can_contact,
            short_description=short_description
        )
        self._pydantic_agent = pydantic_agent
        self._dependencies = None
        self._add_mahilo_tools()
        self.add_system_prompt()
        self._instructions = """
            If you have asked another agent a question already, do not ask it again.
            Wait until they reach out to you actively. Meanwhile, you can just wait and
            let the user know that you are waiting for the other agent to respond.
            """

    def _add_mahilo_tools(self):
        """Add the mahilo tools to the PydanticAI agent."""
        _ = get_chat_with_agent_tool_pydanticai(self._pydantic_agent)

    def activate(self, server_id: str = None, dependencies: Any = None) -> None:
        """Activate the PydanticAI agent."""
        super().activate(server_id, dependencies)
        self._dependencies = dependencies

    def add_system_prompt(self) -> None:
        """Add a system prompt to the PydanticAI agent."""
        @self._pydantic_agent.system_prompt
        def system_prompt_func(ctx: RunContext[Any]) -> str:
            return self.description + "\n" + self._instructions
        
    async def process_chat_message(self, message: str = None, websockets: List[WebSocket] = []) -> Dict[str, Any]:
        """Process a message using the PydanticAI agent's run method."""
        if not message:
            return {"response": "", "activated_agents": []}

        # Get context from other agents
        other_agent_messages = self._agent_manager.get_agent_messages(self.name, num_messages=7)
        
        message_full = f"{other_agent_messages}"
        available_agents = self.get_contactable_agents_with_description()
        if message:
            message_full += f"\nUser: {message}"
            console.print("[bold blue]🤖 Available Agents:[/bold blue]")
            for agent_type, desc in available_agents.items():
                console.print(f"  [green]▪[/green] [cyan]{agent_type}:[/cyan] [dim]{desc}[/dim]")
            message_full += f"\n Available agents to chat with: {available_agents}"
            message_full += f"\n Your Agent Name: {self.name}"
        
        print("System prompts:", self._pydantic_agent._system_prompts)
        print("Function tools:", self._pydantic_agent._function_tools)
        # Run the PydanticAI agent
        result = await self._pydantic_agent.run(message_full, deps=self._dependencies)
        
        # Convert the Pydantic model result to a string response
        response_text = str(result.data)

        # Get activated agents
        activated_agents = [
            agent.name for agent in self._agent_manager.get_all_agents() 
            if agent.is_active() and agent.name != self.name
        ]

        # Store the message and response in the session
        self._session.add_message(message, "user")
        self._session.add_message(response_text, "assistant")

        print("Activated agents:", activated_agents)
        print(f"In process_chat_message: Response for {self.name}: {response_text}")

        return {
            "response": response_text,
            "activated_agents": activated_agents
        }

    async def process_queue_message(self, websockets: List[WebSocket] = []) -> None:
        """Process messages from the broker queue."""
        # Get pending messages from broker
        pending_messages = self._agent_manager.message_broker.get_pending_messages(self.name)
        
        for envelope in pending_messages:
            try:
                # Verify message if signed
                if self._agent_manager.message_broker.secret_key:
                    if not envelope.verify(self._agent_manager.message_broker.secret_key):
                        print(f"Warning: Message {envelope.message_id} failed signature verification")
                        continue

                # Format message for processing
                formatted_message = f"{envelope.sender}: {envelope.payload}"
                message_full = f"Pending messages: {formatted_message}"
                print(f"Queue message for {self.name}: {message_full}")
                
                available_agents = self.get_contactable_agents_with_description()
                message_full += f"\n Available agents to chat with: {available_agents}"
                message_full += f"\n Your Agent Name: {self.name}"
                
                # Run the PydanticAI agent
                result = await self._pydantic_agent.run(message_full, deps=self._dependencies)
                response_text = str(result.data)

                # Store the message and response in the session
                self._session.add_message(formatted_message, "user")
                self._session.add_message(response_text, "assistant")

                # Acknowledge successful processing
                self._agent_manager.message_broker.acknowledge_message(
                    envelope.message_id, self.name
                )
                
                print(f"In process_queue_message: Response for {self.name}: {response_text}")
                
            except Exception as e:
                print(f"Error processing message {envelope.message_id}: {e}")
                # Handle failure and retry if needed
                should_retry = self._agent_manager.message_broker.handle_failure(
                    envelope.message_id, self.name
                )
                if not should_retry:
                    print(f"Max retries exceeded for message {envelope.message_id}")
                    # Could send error message back to sender here