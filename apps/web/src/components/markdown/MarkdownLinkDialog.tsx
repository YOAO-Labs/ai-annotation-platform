import {
  activeEditor$,
  cancelLinkEdit$,
  editorRootElementRef$,
  iconComponentFor$,
  linkDialogState$,
  onWindowChange$,
  readOnly$,
  removeLink$,
  switchFromPreviewToLinkEdit$,
  updateLink$,
  useCellValues,
  usePublisher,
  useTranslation,
  type EditLinkDialog,
  type PreviewLinkDialog,
} from "@mdxeditor/editor";
import { Popover } from "radix-ui";
import {
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type Ref,
} from "react";

import styles from "./MarkdownEditor.module.css";

export const MarkdownLinkSuggestions = createContext<readonly string[]>([]);

function LinkForm({
  state,
  addressRef,
}: {
  state: EditLinkDialog;
  addressRef: Ref<HTMLInputElement>;
}) {
  const t = useTranslation();
  const updateLink = usePublisher(updateLink$);
  const cancel = usePublisher(cancelLinkEdit$);
  const suggestions = useContext(MarkdownLinkSuggestions);
  const id = useId();
  const listId = `${id}-suggestions`;
  const [url, setUrl] = useState(state.url);
  const [showOptions, setShowOptions] = useState(true);
  const [highlight, setHighlight] = useState(-1);
  const matches: string[] = [];
  const query = url.toLowerCase();
  for (const value of suggestions) {
    if (value.toLowerCase().includes(query)) matches.push(value);
    if (matches.length === 20) break;
  }
  const expanded = showOptions && matches.length > 0;
  const activeOption = expanded ? matches[highlight] : undefined;

  useEffect(() => {
    if (activeOption !== undefined) {
      document.getElementById(`${listId}-${highlight}`)?.scrollIntoView?.({ block: "nearest" });
    }
  }, [activeOption, highlight, listId]);

  const choose = (value: string) => {
    setUrl(value);
    setShowOptions(false);
    setHighlight(-1);
  };

  return (
    <form
      className={styles.linkForm}
      onSubmit={(event) => {
        event.preventDefault();
        event.stopPropagation();
        const data = new FormData(event.currentTarget);
        updateLink({
          url,
          title: String(data.get("title") ?? ""),
          text: String(data.get("text") ?? ""),
        });
      }}
    >
      <label id={`${id}-label`} htmlFor={id}>
        {t("createLink.url", "URL")}
      </label>
      <input
        id={id}
        ref={addressRef}
        value={url}
        placeholder={t("createLink.urlPlaceholder", "Select or paste an URL")}
        {...(suggestions.length > 0 && {
          role: "combobox",
          "aria-autocomplete": "list" as const,
          "aria-expanded": expanded,
          "aria-controls": listId,
          "aria-activedescendant":
            activeOption === undefined ? undefined : `${listId}-${highlight}`,
          autoComplete: "off",
          onClick: () => setShowOptions(true),
        })}
        onChange={(event) => {
          setUrl(event.target.value);
          setShowOptions(true);
          setHighlight(-1);
        }}
        onBlur={() => {
          setShowOptions(false);
          setHighlight(-1);
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || matches.length === 0) return;
          if (event.key === "ArrowDown" || event.key === "ArrowUp") {
            event.preventDefault();
            setShowOptions(true);
            if (event.key === "ArrowDown") {
              setHighlight(highlight < matches.length - 1 ? highlight + 1 : 0);
            } else {
              setHighlight(highlight > 0 ? highlight - 1 : matches.length - 1);
            }
          } else if (event.key === "Enter" && expanded) {
            event.preventDefault();
            if (activeOption !== undefined) choose(activeOption);
            else setShowOptions(false);
          }
        }}
      />
      {suggestions.length > 0 && (
        <ul
          id={listId}
          role="listbox"
          aria-labelledby={`${id}-label`}
          hidden={!expanded}
          className={styles.linkSuggestions}
        >
          {expanded &&
            matches.map((value, index) => (
              <li
                id={`${listId}-${index}`}
                key={index}
                role="option"
                aria-selected={index === highlight}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => choose(value)}
              >
                {value}
              </li>
            ))}
        </ul>
      )}
      {state.withAnchorText && (
        <label>
          {t("createLink.text", "Anchor text")}
          <input name="text" defaultValue={state.text} />
        </label>
      )}
      <label>
        {t("createLink.title", "Link title")}
        <input name="title" defaultValue={state.title} />
      </label>
      <div className={styles.linkActions}>
        <button type="submit" aria-label={t("createLink.saveTooltip", "Set URL")}>
          {t("dialogControls.save", "Save")}
        </button>
        <button type="button" onClick={() => cancel()}>
          {t("dialogControls.cancel", "Cancel")}
        </button>
      </div>
    </form>
  );
}

function LinkPreview({ state }: { state: PreviewLinkDialog }) {
  const t = useTranslation();
  const icon = useCellValues(iconComponentFor$)[0];
  const edit = usePublisher(switchFromPreviewToLinkEdit$);
  const remove = usePublisher(removeLink$);
  const [copied, setCopied] = useState(false);
  const copyTimer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(copyTimer.current), []);
  const external = state.url.startsWith("http");
  const openLabel = external
    ? t("linkPreview.open", "Open {{url}} in new window", { url: state.url })
    : state.url;
  const editLabel = t("linkPreview.edit", "Edit link URL");
  const copyLabel = t("linkPreview.copyToClipboard", "Copy to clipboard");
  const removeLabel = t("linkPreview.remove", "Remove link");

  return (
    <div className={styles.linkActions}>
      <a
        data-testid="link-dialog-preview"
        href={state.href ?? "about:blank"}
        title={openLabel}
        {...(external && { target: "_blank", rel: "noreferrer" })}
      >
        <span>{state.url}</span>
        {external && icon("open_in_new")}
      </a>
      <button type="button" title={editLabel} aria-label={editLabel} onClick={() => edit()}>
        {icon("edit")}
      </button>
      <button
        type="button"
        title={copyLabel}
        aria-label={copyLabel}
        onClick={async () => {
          await navigator.clipboard.writeText(state.url);
          window.clearTimeout(copyTimer.current);
          setCopied(true);
          copyTimer.current = window.setTimeout(() => setCopied(false), 1000);
        }}
      >
        {icon(copied ? "check" : "content_copy")}
      </button>
      <button type="button" title={removeLabel} aria-label={removeLabel} onClick={() => remove()}>
        {icon("link_off")}
      </button>
      <span role="status" className={copied ? styles.linkCopyStatus : "sr-only"}>
        {copied && t("linkPreview.copied", "Copied!")}
      </span>
    </div>
  );
}

export function MarkdownLinkDialog() {
  const [state, editor, portal, readOnly] = useCellValues(
    linkDialogState$,
    activeEditor$,
    editorRootElementRef$,
    readOnly$,
  );
  const t = useTranslation();
  const cancel = usePublisher(cancelLinkEdit$);
  const setState = usePublisher(linkDialogState$);
  const refresh = usePublisher(onWindowChange$);
  const contentRef = useRef<HTMLDivElement>(null);
  const addressRef = useRef<HTMLInputElement>(null);
  const preview = state.type === "preview";

  useEffect(() => {
    if (!preview || readOnly || !editor) return;
    const update = () => editor.getEditorState().read(() => refresh(true));
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [editor, preview, readOnly, refresh]);

  if (readOnly || state.type === "inactive" || !portal?.current) return <></>;
  const { top, left, width, height } = state.rectangle;
  const editMode = state.type === "edit";
  const dialogLabel =
    state.type === "preview"
      ? t("linkPreview.open", "Open link")
      : state.initialUrl
        ? t("linkPreview.edit", "Edit link")
        : t("toolbar.link", "Insert link");
  return (
    <Popover.Root open>
      <Popover.Anchor
        aria-hidden
        className={styles.linkAnchor}
        // eslint-disable-next-line no-restricted-syntax -- Selection geometry is only known in the browser.
        style={
          {
            "--link-anchor-top": `${top}px`,
            "--link-anchor-left": `${left}px`,
            "--link-anchor-width": `${width}px`,
            "--link-anchor-height": `${height}px`,
          } as CSSProperties
        }
      />
      <Popover.Portal container={portal.current}>
        <Popover.Content
          key={state.type}
          ref={contentRef}
          className={styles.linkPopover}
          aria-label={dialogLabel}
          sideOffset={5}
          updatePositionStrategy="always"
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            if (editMode) addressRef.current?.focus();
          }}
          onCloseAutoFocus={(event) => event.preventDefault()}
          onEscapeKeyDown={(event) => {
            event.preventDefault();
            event.stopPropagation();
            if (event.isComposing) return;
            if (editMode) cancel();
            else {
              if (contentRef.current?.contains(document.activeElement)) editor?.focus();
              setState({ type: "inactive" });
            }
          }}
        >
          {state.type === "edit" ? (
            <LinkForm state={state} addressRef={addressRef} />
          ) : (
            <LinkPreview key={state.url} state={state} />
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
