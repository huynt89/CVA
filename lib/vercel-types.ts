export type VercelRequest = {
  method?: string;
  headers: Record<string, string | string[] | undefined>;
  body?: unknown;
};

export type VercelResponse = {
  status(code: number): VercelResponse;
  json(body: unknown): VercelResponse;
};
