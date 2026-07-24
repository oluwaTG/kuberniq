export interface User {
  username: string;
  role: "admin" | "operator" | "viewer";
  allowed_namespaces: string[];
  role_description?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  endpoints?: string[];
  rawContext?: string;     // only when debug mode is on
  isStreaming?: boolean;
  error?: string;
}

export interface Model {
  id: string;
  name: string;
  provider: string;
}

export interface ManagedUser {
  username: string;
  role: string;
  allowed_namespaces?: string[];
}
