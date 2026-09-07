import { expect, it } from "vitest";
import { API_VERSION } from "./index";

it("exports API version", () => expect(API_VERSION).toBe("v1"));
