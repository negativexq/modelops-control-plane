"use client";

import { useState } from "react";
import { UserRound } from "lucide-react";
import { OPERATOR_PATTERN, setOperator, useOperator } from "@/lib/operator";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";

/** Who is at the console. The name goes on every manual action's audit event. */
export function OperatorMenu() {
  const operator = useOperator();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const trimmed = draft.trim();
  const valid = trimmed === "" || OPERATOR_PATTERN.test(trimmed);

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) setDraft(operator ?? "");
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className={cn("h-7 gap-1.5 px-2 text-xs", !operator && "text-warning")}
          data-testid="operator-menu"
        >
          <UserRound className="size-3.5" />
          <span className="max-w-32 truncate">{operator ?? "Set your name"}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72">
        <form
          className="space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            if (!valid) return;
            setOperator(trimmed || null);
            setOpen(false);
          }}
        >
          <div className="space-y-1.5">
            <Label htmlFor="operator-name">Operator name</Label>
            <Input
              id="operator-name"
              autoFocus
              value={draft}
              maxLength={64}
              placeholder="e.g. Ada Lovelace"
              aria-invalid={!valid}
              onChange={(event) => setDraft(event.target.value)}
            />
            <p className={cn("text-xs", valid ? "text-muted-foreground" : "text-danger")}>
              {valid
                ? "Recorded on promotes, rollbacks and pauses you make. Not a login: anyone can type any name."
                : "Letters, digits, spaces and . @ ' - _ only."}
            </p>
          </div>
          <div className="flex justify-end gap-2">
            {operator ? (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => {
                  setOperator(null);
                  setOpen(false);
                }}
              >
                Clear
              </Button>
            ) : null}
            <Button type="submit" size="sm" disabled={!valid}>
              Save
            </Button>
          </div>
        </form>
      </PopoverContent>
    </Popover>
  );
}
