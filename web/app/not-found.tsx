import Link from "next/link";

export default function NotFound() {
  return (
    <div className="state state-empty">
      <h2>Nothing here</h2>
      <p>This page does not exist, or it belongs to another business.</p>
      <Link className="button" href="/review">
        Back to the review deck
      </Link>
    </div>
  );
}
