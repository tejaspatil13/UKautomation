import { useState } from "react";
import { BusinessRow } from "./api";
import { gbp } from "./ui";

export function Phone({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="dash">—</span>;
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      /* clipboard blocked: the number is still selectable */
    }
  }
  return (
    <button type="button" className="phone" onClick={copy} title="Copy number">
      <span>{value}</span>
      <span className="phone-hint" aria-live="polite">{copied ? "Copied" : "Copy"}</span>
    </button>
  );
}

/** Business centre / shared offices: the name shown is only the most likely business in the building. */
export function SharedTag({ row }: { row: BusinessRow }) {
  if (row.unit_company) {
    return (
      <div className="unit-tag" title={`Companies House lists ${row.unit_company} at ${row.number_or_name}`}>
        Unit confirmed · Companies House
      </div>
    );
  }
  if (!row.shared_building) return null;
  const unit = row.properties.length === 1 ? row.number_or_name : "these units";
  return (
    <div className="shared-tag" title={`The listings show ${row.shared_building}${row.building_businesses ? "" : " or more"} businesses in this building but not which unit each is in. Ask who is in ${unit}.`}>
      {row.occupier_unknown ? "Business centre" : "Shared building"} · {row.shared_building}{row.building_businesses ? "" : "+"} businesses
    </div>
  );
}

const RV_SHOWN = 3; // more than this: the rest behind "+ N more"

/** Every rateable value the business has, with a link to each one's Gov.uk record underneath. */
export function RateableValues({ row }: { row: BusinessRow }) {
  const [all, setAll] = useState(false);
  const props = row.properties;
  const shown = all || props.length <= RV_SHOWN ? props : props.slice(0, RV_SHOWN);
  const hidden = props.length - shown.length;
  return (
    <div className="rvs">
      {shown.map((p) => <span key={p.uarn} className="rv-value">{gbp(p.rv)}</span>)}
      {props.length > RV_SHOWN && (
        <button type="button" className="link-btn rv-more" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? "Show less" : `+ ${hidden} more`}
        </button>
      )}
      {props.length === 1 ? (
        <a className="sub chip-link" href={props[0].source_url} target="_blank" rel="noreferrer">Gov.uk record ↗</a>
      ) : (
        <span className="sub gov-links">
          Gov.uk records:{" "}
          {shown.map((p, i) => (
            <a key={p.uarn} href={p.source_url} target="_blank" rel="noreferrer" title={`${gbp(p.rv)} · ${p.address}`}>{i + 1}</a>
          ))}
        </span>
      )}
    </div>
  );
}

export function RecordsTable({ items, onRemove, empty }: {
  items: BusinessRow[]; onRemove?: (row: BusinessRow) => void; empty: string;
}) {
  const cols = 4 + (onRemove ? 1 : 0);
  return (
    <div className="table-wrap">
      <table className="records">
        <thead>
          <tr>
            <th className="col-biz">Business</th>
            <th className="col-phone">Phone</th>
            <th>Address</th>
            <th className="num col-rv">Rateable value</th>
            {onRemove && <th className="col-remove"><span className="sr-only">Remove</span></th>}
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.key}>
              <td data-label="Business">
                <div className="biz">{r.business_name || <span className="dash">{r.occupier_unknown ? "Occupier not known" : "No business found"}</span>}</div>
                <div className="sub">{r.description}</div>
                <SharedTag row={r} />
              </td>
              <td data-label="Phone">
                <Phone value={r.phone} />
                {r.phone_label && <div className="sub">{r.phone_label}</div>}
              </td>
              <td data-label="Address">
                <div>{r.business_address || r.address}</div>
                {(r.shared_building > 0 || r.unit_company) && <div className="sub unit">{r.unit_company ? r.number_or_name : `Ask for: ${r.number_or_name}`}</div>}
                <a href={r.google_maps_url} target="_blank" rel="noreferrer" className="sub chip-link">Google Maps ↗</a>
              </td>
              <td data-label="Rateable value" className="num"><RateableValues row={r} /></td>
              {onRemove && (
                <td className="remove-cell">
                  <button type="button" className="icon-btn" onClick={() => onRemove(r)} aria-label={`Remove ${r.business_name || r.address}`} title="Remove">×</button>
                </td>
              )}
            </tr>
          ))}
          {items.length === 0 && (
            <tr className="empty-row"><td colSpan={cols}>{empty}</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
